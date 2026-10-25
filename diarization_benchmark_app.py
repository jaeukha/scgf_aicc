#!/usr/bin/env python3
"""Compare Whisper Turbo and the deployed Qwen ASR on one diarized recording.

Run: .\\.venv\\Scripts\\python.exe diarization_benchmark_app.py
The Hugging Face token is read from HF_TOKEN or entered only for the current run.
"""
from __future__ import annotations

import gc
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
RATE = 16000
QWEN_MODEL = "qwen3-asr-0.6b"
QWEN_ID = "Qwen/Qwen3-ASR-0.6B-hf"
QWEN_REVISION = "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c"


def decode_audio(path: str) -> np.ndarray:
    """Decode locally without copying the uploaded recording into this project."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
        capture_output=True, check=False, timeout=180,
    )
    if result.returncode or not result.stdout:
        raise ValueError("오디오를 읽지 못했습니다. WAV, M4A, MP3 파일을 확인하세요.")
    return np.frombuffer(result.stdout, dtype=np.float32)


def diarize(samples: np.ndarray, token: str) -> tuple[list[dict], float]:
    if not token:
        raise ValueError("화자 분리에는 Hugging Face 토큰이 필요합니다. HF_TOKEN을 설정하거나 아래에 입력하세요.")
    import torch
    from pyannote.audio import Pipeline

    started = time.perf_counter()
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=token)
    pipeline.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
    output = pipeline({"waveform": torch.from_numpy(samples[None]), "sample_rate": RATE})
    annotation = getattr(output, "exclusive_speaker_diarization", output.speaker_diarization)
    turns = [{"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": str(speaker)}
             for turn, _, speaker in annotation.itertracks(yield_label=True) if turn.end - turn.start >= 0.3]
    del pipeline
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return turns, time.perf_counter() - started


def qwen_transcribe(samples: np.ndarray, turns: list[dict]) -> tuple[list[str], float]:
    """Use the same Qwen runtime and model folder as the currently deployed service."""
    import app as legacy

    legacy.ASR_ID = QWEN_ID
    settings = legacy.Settings(ROOT / "models" / "decider-flash-v1.2", asr_revision=QWEN_REVISION,
                               asr_dir=ROOT / "models" / QWEN_MODEL, asr_device="cuda")
    runtime = legacy.Runtime(settings)
    asr = legacy.ASRRuntime(runtime)
    started, texts = time.perf_counter(), []
    try:
        for turn in turns:
            part = samples[round(turn["start"] * RATE):round(turn["end"] * RATE)]
            text, _ = asr.transcribe(part, "한국어", None)
            texts.append(text)
    finally:
        asr.unload()
    return texts, time.perf_counter() - started


def whisper_transcribe(samples: np.ndarray, turns: list[dict]) -> tuple[list[str], float]:
    import torch
    import whisper

    started = time.perf_counter()
    model = whisper.load_model("turbo", device="cuda" if torch.cuda.is_available() else "cpu",
                               download_root=str(ROOT / "models" / "whisper-turbo"))
    try:
        texts = [model.transcribe(samples[round(turn["start"] * RATE):round(turn["end"] * RATE)],
                                  language="ko", task="transcribe", fp16=torch.cuda.is_available(), verbose=False)["text"].strip()
                 for turn in turns]
    finally:
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return texts, time.perf_counter() - started


def result_rows(turns: list[dict], qwen: list[str], whisper: list[str]) -> list[list[str]]:
    return [[f"{turn['start']:.2f}–{turn['end']:.2f}", turn["speaker"], qwen[index], whisper[index]]
            for index, turn in enumerate(turns)]


def compare(path: str, token: str):
    if not path:
        raise ValueError("비교할 음성 파일을 선택하세요.")
    samples = decode_audio(path)
    token = token.strip() or os.environ.get("HF_TOKEN", "")
    turns, diarization_seconds = diarize(samples, token)
    if not turns:
        raise ValueError("화자 발화를 찾지 못했습니다.")
    qwen, qwen_seconds = qwen_transcribe(samples, turns)
    whisper, whisper_seconds = whisper_transcribe(samples, turns)
    duration = len(samples) / RATE
    speakers = Counter(turn["speaker"] for turn in turns)
    summary = (
        f"### 비교 결과\n\n"
        f"- 음성 길이: **{duration:.1f}초**, 공통 화자 분리: **{diarization_seconds:.1f}초**\n"
        f"- 감지 화자: **{len(speakers)}명**, 분리 발화: **{len(turns)}개**\n"
        f"- Qwen3-ASR-0.6B: **{qwen_seconds:.1f}초** (전사만, {duration / qwen_seconds:.2f}× 실시간)\n"
        f"- Whisper Turbo: **{whisper_seconds:.1f}초** (전사만, {duration / whisper_seconds:.2f}× 실시간)\n\n"
        f"> 화자 번호와 전환 시점은 두 열 모두 동일한 pyannote 결과입니다. 두 ASR 모델에는 자체 화자 분리 API가 없으므로, 이 표는 화자별 전사 품질과 속도 비교입니다."
    )
    return summary, result_rows(turns, qwen, whisper)


def build_app():
    import gradio as gr

    with gr.Blocks(title="AICC 화자 분리 전사 비교") as demo:
        gr.Markdown("# Whisper Turbo · Qwen3-ASR 비교\n동일한 pyannote 화자 분리 결과에 두 전사 모델을 순차 적용합니다.")
        with gr.Row():
            audio = gr.Audio(label="비교할 음성", type="filepath")
            token = gr.Textbox(label="Hugging Face 토큰 (현재 실행에만 사용)", type="password", value="")
        run = gr.Button("화자 분리 및 비교 실행", variant="primary")
        summary = gr.Markdown()
        table = gr.Dataframe(headers=["구간", "화자", "Qwen3-ASR-0.6B", "Whisper Turbo"], interactive=False)
        run.click(compare, [audio, token], [summary, table])
    return demo


if __name__ == "__main__":
    build_app().launch(server_name="127.0.0.1", server_port=7861)
