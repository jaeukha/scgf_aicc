"""모델 서버: app.py의 Decider·ASR(·선택적 Coach) 런타임을 /internal/* HTTP API로 노출한다.

GPU 전용 venv(app.py --setup)에서 실행한다. AICC 백엔드는 PROVIDER_MODE=local로 이 서버를 호출한다.
  .\\.venv\\Scripts\\python.exe model_server.py --offline --port 8101            # ASR + Decider
  .\\.venv\\Scripts\\python.exe model_server.py --offline --port 8101 --coach      # + Coach(Qwen3.5-2B NF4)
요청 본문·전사는 로그에 남기지 않는다.
"""
import argparse
import io
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import app as legacy

ROOT = Path(__file__).resolve().parent
# 16GB GPU에서 Coach까지 함께 올릴 때는 ASR 0.6B를 쓴다(1.7B면 VRAM이 넘쳐 Decider가 수십 배 느려짐)
WHISPERX_MODEL = "turbo"
WHISPERX_VERSION = "whisperx-3.8.6"
CHUNK_SECONDS = 0.5  # 기존 파일 재생과 같은 0.5초 입력 단위
DIARIZATION_CHUNK_SECONDS = 15  # 긴 파일도 롤링 화자 클러스터를 이어 가는 단위


_strict_parse = legacy.parse_guidance


class WhisperXRuntime:
    """WhisperX Turbo adapter preserving the existing ASR result contract."""
    def __init__(self, device, offline=False):
        self.device, self.offline, self.model = device, offline, None

    def load(self):
        if self.model is None:
            import whisperx
            self.model = whisperx.load_model(WHISPERX_MODEL, self.device,
                                              compute_type="float16" if self.device == "cuda" else "int8",
                                              language="ko", download_root=str(ROOT / "models" / "whisperx"),
                                              local_files_only=self.offline)
        return self.status()

    def status(self):
        return f"STT {'loaded' if self.model is not None else 'not loaded'} · WhisperX Turbo · {self.device}"

    def transcribe(self, audio, language, prompt):
        self.load()
        started = time.perf_counter()
        # The caller already supplies a speech-only, diarized slice; rerunning WhisperX VAD here is prohibitively slow.
        segments, _ = self.model.model.transcribe(audio, language="ko", beam_size=5, vad_filter=False)
        return " ".join(segment.text.strip() for segment in segments).strip(), time.perf_counter() - started


def _escape_inner_quotes(raw):
    """값 문자열 안의 큰따옴표를 이스케이프. 여는 따옴표({ [ , : 뒤)와 닫는 따옴표(, : } ] 앞)는 구조로 보고 남긴다."""
    import re
    return re.sub(r'(?<![{\[,:])(?<![{\[,:]\s)"(?!\s*[,:}\]])', r'\\"', raw)


def _merged_objects(raw):
    """JSON 객체 하나, 또는 줄마다 나뉜 여러 객체({..}\n{..})를 하나로 합친다. 실패하면 None."""
    import json
    decoder, pos, merged = json.JSONDecoder(), 0, {}
    try:
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \t\r\n,":
                pos += 1
            if pos >= len(raw):
                break
            obj, pos = decoder.raw_decode(raw, pos)
            if not isinstance(obj, dict):
                return None
            merged.update(obj)
    except ValueError:
        return None
    return merged or None


def decode_audio(data):
    """libsndfile가 못 읽는 스트림 형식은 FFmpeg로 메모리에서 PCM으로 변환한다.
    M4A는 컨테이너 seek이 필요할 수 있어 웹 UI에서 WAV로 변환해 보낸다.
    """
    import numpy as np
    import soundfile as sf
    try:
        return sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception:
        try:
            result = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                                    input=data, capture_output=True, timeout=120, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise ValueError("AUDIO_FORMAT_UNSUPPORTED") from exc
        if result.returncode or not result.stdout:
            raise ValueError("AUDIO_FORMAT_UNSUPPORTED")
        return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 1), 16000


def diarize_jobs(diarizer, session_id, samples, jobs):
    """Split an ASR job at diarization turns so a line never contains two speakers."""
    pieces = []
    width = DIARIZATION_CHUNK_SECONDS * 16000
    for start_sample in range(0, len(samples), width):
        start, end = start_sample / 16000, min(len(samples), start_sample + width) / 16000
        selected = [job for job in jobs if job.start < end and job.end > start]
        if not selected:
            continue
        turns = diarizer.turns(session_id, samples[start_sample:int(end * 16000)])
        for job in selected:
            job_start, job_end = max(start, job.start), min(end, job.end)
            matches = [(max(job_start - start, turn["start"]), min(job_end - start, turn["end"]), turn["speaker"])
                       for turn in turns if turn["start"] < job_end - start and turn["end"] > job_start - start]
            if not matches:
                matches = [(job_start - start, job_end - start, "unknown")]
            for piece_start, piece_end, speaker in matches:
                if piece_end - piece_start < 0.3:
                    continue
                offset_start = round((start + piece_start - job.start) * 16000)
                offset_end = round((start + piece_end - job.start) * 16000)
                pieces.append((SimpleNamespace(samples=job.samples[offset_start:offset_end],
                                                start=start + piece_start, end=start + piece_end), speaker))
    return pieces


def tolerant_parse(text):
    """Qwen3.5-2B가 형식만 어기는 경우(앞뒤 설명문, 코드펜스, 추가 키, 업무 3개 이상)를 정리한 뒤
    원래의 엄격한 검증(빈 값·길이·thinking 토큰)을 그대로 적용한다. 내용 검증은 완화하지 않는다."""
    import json
    import re
    try:
        return _strict_parse(text)
    except ValueError as first:
        match = re.search(r"\{.*\}", text or "", re.S)
        data = None
        for candidate in ([match.group(0), _escape_inner_quotes(match.group(0))] if match else []):
            data = _merged_objects(candidate)
            if data:
                break
        if not data:
            raise first
        print("coach format repaired: keys=", sorted(data), flush=True)  # 내용은 남기지 않음
        actions = data.get("action_items")
        data = {"staff_note": data.get("staff_note", ""), "customer_question": data.get("customer_question") or "",
                "action_items": actions[:2] if isinstance(actions, list) else []}
        return _strict_parse(json.dumps(data, ensure_ascii=False))


legacy.parse_guidance = tolerant_parse  # GuidanceRuntime.generate가 모듈 전역 이름으로 호출
# QA에서 본 실패 유형(문자열 안 큰따옴표로 JSON 깨짐, 고객 질문 칸에 직원 지시·고객 발화)을 줄이기 위한 보충 규칙
# 프롬프트 보충 규칙은 시도했지만 Qwen3.5-2B에서 성공률이 오히려 떨어졌다(82%→73%, 샘플 22발화 실측).
# 원래 COACH_SYSTEM을 유지하고 형식 보정만 한다. 더 큰 모델로 바꿀 때 다시 검토.


def build(args):
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from pydantic import BaseModel

    asr_id, asr_rev = "WhisperX/turbo", WHISPERX_VERSION
    legacy.ASR_ID = asr_id  # app.validate_asr가 모델 ID를 이 상수와 비교한다
    settings = legacy.Settings(ROOT / "models" / "decider-flash-v1.2", offline=args.offline,
                               max_length=args.max_length, asr_device=args.asr_device,
                               coach_backend="local" if args.coach else "off", coach_timeout=args.coach_timeout)
    runtime = legacy.Runtime(settings)
    asr = WhisperXRuntime(args.asr_device, args.offline)
    coach = legacy.GuidanceRuntime(runtime) if args.coach else None
    diarizer = LiveDiarizer(args) if args.diarize else None
    api = FastAPI(title="AICC model server")

    @api.on_event("startup")
    def load():
        if args.autoload:
            print(runtime.load()), print(asr.load())
            if coach:
                print(coach.load())

    @api.get("/health")
    def health():
        return {"decider": runtime.model is not None, "asr": asr.model is not None,
                "coach": None if coach is None else coach.model is not None,
                "diarization": None if diarizer is None else diarizer.status,
                "versions": {"decider": f"{legacy.MODEL_ID}@{settings.revision}",
                             "asr": f"{asr_id}@{asr_rev}",
                             "coach": f"{legacy.COACH_ID}@{settings.coach_revision}" if coach else None}}

    class DecideIn(BaseModel):
        state: dict
        questions: dict
        layout: str = "공동 판정 (joint)"

    @api.post("/internal/decider/evaluate")
    def decide(body: DecideIn):
        try:
            result, seconds = runtime.decide(body.state, body.questions, [], body.layout, None)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(503, type(exc).__name__) from None
        return result | {"seconds": round(seconds, 3)}

    @api.post("/internal/asr/transcribe")
    def transcribe(file: UploadFile = File(...), language: str = Form("한국어"), threshold: float = Form(0.012),
                   silence: float = Form(0.7), max_seconds: float = Form(12),
                   diarization_session_id: str = Form("")):
        """오디오 → RMS 구간 분할 → ASR. --diarize면 메모리 롤링 윈도우로 화자 번호도 붙인다."""
        import numpy as np
        try:
            data, rate = decode_audio(file.file.read())
        except Exception:
            raise HTTPException(415, "AUDIO_FORMAT_UNSUPPORTED") from None
        seg = legacy.Segmenter(threshold, silence, max_seconds)
        jobs, step = [], max(1, int(rate * CHUNK_SECONDS))
        mono = []
        for pos in range(0, len(data), step):
            samples = legacy.pcm_to_mono16((rate, data[pos:pos + step]))
            mono.append(samples)
            jobs += seg.feed(samples, 0)
        last = seg.flush(0)
        jobs += [last] if last else []
        diarized_jobs = [(job, "unknown") for job in jobs]
        diarization_seconds = 0.0
        if diarizer is not None and diarization_session_id and jobs:
            diarization_started = time.perf_counter()
            diarized_jobs = diarize_jobs(diarizer, diarization_session_id, np.concatenate(mono), jobs)
            diarization_seconds = time.perf_counter() - diarization_started
        out, started = [], time.perf_counter()
        for job, speaker in diarized_jobs:
            if len(job.samples) < 16000 * 0.3:  # 0.3초 미만 잡음 구간 제외
                continue
            text, _ = asr.transcribe(job.samples, language, None)
            if text:
                out.append({"start": round(job.start, 2), "end": round(job.end, 2), "text": text, "speaker": speaker})
        return {"segments": out, "seconds": round(time.perf_counter() - started, 3),
                "diarization_seconds": round(diarization_seconds, 3), "audio_seconds": round(len(data) / rate, 2),
                "diarization": None if diarizer is None else diarizer.status}

    @api.post("/internal/coach/generate")
    def generate(body: dict):
        if coach is None:
            raise HTTPException(503, "coach disabled")
        try:
            guidance, seconds = coach.generate(body["context"], lambda: False)
        except ValueError as exc:  # parse_guidance 형식 오류·시간 초과
            print("coach rejected:", str(exc)[:120], flush=True)  # 고정 문구라 상담 내용은 남지 않음
            raise HTTPException(502, str(exc)[:200]) from None
        return {"guidance": guidance, "seconds": round(seconds, 3)}

    return api


class LiveDiarizer:
    """원본을 디스크에 쓰지 않고 최근 오디오만 보관해 화자 클러스터 번호를 유지한다."""
    def __init__(self, args):
        self.args, self.pipeline, self.sessions = args, None, {}
        self.lock = threading.Lock()
        self.status = "준비 전"

    def _load(self):
        if self.pipeline is not None:
            return
        token = os.environ.get(self.args.hf_token_env)
        if not token:
            raise RuntimeError(f"{self.args.hf_token_env} 환경변수가 필요합니다.")
        from whisperx.diarize import DiarizationPipeline
        self.pipeline = DiarizationPipeline(model_name=self.args.diarization_model, token=token,
                                            device=self.args.asr_device)
        self.status = "준비됨"

    def turns(self, session_id, samples):
        """현재 요청의 각 ASR 구간에 speaker_1/2를 붙인다. 역할(고객/상담원)은 단정하지 않는다."""
        import numpy as np
        from aicc.diarization import StableSpeakerLabels
        with self.lock:
            try:
                self._load()
                state = self.sessions.setdefault(session_id, {"samples": np.empty(0, dtype=np.float32), "cursor": 0.0,
                                                               "labels": StableSpeakerLabels()})
                base = state["cursor"]
                state["cursor"] += len(samples) / 16000
                state["samples"] = np.concatenate((state["samples"], samples))[-int(self.args.diarization_window * 16000):]
                window_start = state["cursor"] - len(state["samples"]) / 16000
                output = self.pipeline(state["samples"])
                turns = [{"start": window_start + row.start, "end": window_start + row.end, "speaker": row.speaker}
                         for row in output.itertuples(index=False)]
                stable = state["labels"].stabilize(turns)
                return [{**turn, "start": max(0.0, turn["start"] - base),
                         "end": min(len(samples) / 16000, turn["end"] - base)}
                        for turn in stable if turn["end"] > base and turn["start"] < state["cursor"]]
            except Exception as exc:
                self.status = f"사용 불가: {type(exc).__name__}"
                return []

    def label(self, session_id, samples, jobs):
        """Compatibility helper for callers that only need one label per job."""
        turns = self.turns(session_id, samples)
        return [max((turn for turn in turns if turn["start"] < job.end and turn["end"] > job.start),
                    key=lambda turn: min(turn["end"], job.end) - max(turn["start"], job.start),
                    default={"speaker": "unknown"})["speaker"] for job in jobs]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8101)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-autoload", dest="autoload", action="store_false")
    p.add_argument("--asr-device", choices=["cuda", "cpu"], default="cuda")
    p.add_argument("--asr-model", choices=["whisperx-turbo"], default="whisperx-turbo")
    p.add_argument("--max-length", type=int, default=8192)
    p.add_argument("--coach", action="store_true", help="Qwen3.5-2B NF4 로컬 Coach 함께 로드")
    p.add_argument("--coach-timeout", type=float, default=20)
    p.add_argument("--diarize", action="store_true", help="pyannote로 자동 화자 분리 (HF_TOKEN 필요)")
    p.add_argument("--diarization-model", default="pyannote/speaker-diarization-community-1")
    p.add_argument("--diarization-window", type=float, default=45, help="메모리에만 유지할 롤링 오디오 초")
    p.add_argument("--hf-token-env", default="HF_TOKEN", help="Hugging Face 토큰 환경변수 이름")
    args = p.parse_args()
    legacy.configure_environment(args.offline)
    import uvicorn
    uvicorn.run(build(args), host=args.host, port=args.port)
