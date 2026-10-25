"""ASR / Decider / Knowledge / Coach Provider.

라우팅·Coach 스키마 검증은 기존 app.py 함수를 그대로 재사용한다(app.py는 최상단에서 무거운 패키지를 import하지 않음).
"""
import contextvars
import io
import json
import re
import wave

import httpx

import app as legacy  # run_routing, validate_routing_config, parse_guidance, COACH_SYSTEM
from . import config
from .telemetry import AppError, span, trace_var

fault_var = contextvars.ContextVar("fault", default={})  # 시연용 오류 주입 {"coach": "timeout"}
MODE = "http" if config.REAL_MODELS else "mock"  # ASR·Decider provider


def maybe_fail(service):
    kind = fault_var.get().get(service)
    if kind == "timeout":
        raise AppError("UPSTREAM_TIMEOUT", f"{service} 응답 시간 초과(주입)", 504)
    if kind == "unavailable":
        raise AppError("UPSTREAM_UNAVAILABLE", f"{service} 연결 실패(주입)", 503)
    if kind == "bad_response":
        raise ValueError("bad response")


def post(url, service, operation, timeout, **kw):
    """모델 서버 HTTP 호출. 연결 실패만 1회 재시도(GPU 작업 타임아웃은 재시도하지 않음)."""
    with span(service, operation, provider=config.PROVIDER_MODE, call_kind="http",
              model_version=config.MODEL_VERSIONS[service]) as extra:
        headers = kw.pop("headers", {}) | {"X-Trace-Id": trace_var.get() or ""}
        for attempt in range(2):
            try:
                r = httpx.post(url, timeout=timeout, headers=headers, **kw)
                break
            except httpx.ConnectError:
                if attempt:
                    raise
                extra["retry_count"] = 1
        extra["http_status_code"] = r.status_code
        extra["request_size_bytes"] = int(r.request.headers.get("content-length", 0)) or None
        extra["response_size_bytes"] = len(r.content)
        if r.status_code >= 400:
            # 모델 서버가 준 사유(고정 문구)를 trace에 남겨 모니터링에서 원인을 볼 수 있게 한다
            try:
                detail = str(r.json().get("detail", ""))[:150]
            except ValueError:
                detail = ""
            if detail and r.status_code != 429:
                code = {503: "UPSTREAM_UNAVAILABLE", 415: "AUDIO_FORMAT_UNSUPPORTED"}.get(r.status_code, "UPSTREAM_BAD_RESPONSE")
                raise AppError(code, f"{service} 응답 오류: {detail}", 502)
        r.raise_for_status()
        return r.json()


def probe(base_url):
    """모델 서버 healthcheck. 실패 시 None."""
    try:
        r = httpx.get(base_url.rstrip("/") + "/health", timeout=2)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# ---------- ASR ----------
MOCK_SCRIPTS = json.loads((config.ROOT / "data" / "mock_transcripts.json").read_text(encoding="utf-8"))


def validate_audio(filename, data):
    name = (filename or "").lower()
    if not name.endswith(config.AUDIO_EXTENSIONS):
        raise AppError("AUDIO_FORMAT_UNSUPPORTED", "지원하지 않는 오디오 형식입니다.", 415)
    if len(data) > config.AUDIO_MAX_BYTES:
        raise AppError("AUDIO_TOO_LARGE", "오디오 파일이 너무 큽니다.", 413)
    if name.endswith(".wav"):
        try:
            with wave.open(io.BytesIO(data)) as w:
                if not (8000 <= w.getframerate() <= 192000 and 1 <= w.getnchannels() <= 8):
                    raise AppError("AUDIO_FORMAT_UNSUPPORTED", "8–192kHz, 1–8채널 WAV만 지원합니다.", 415)
                return round(w.getnframes() / w.getframerate(), 2)
        except (wave.Error, EOFError):
            raise AppError("AUDIO_FORMAT_UNSUPPORTED", "WAV(PCM) 파일을 읽을 수 없습니다.", 415) from None
    return None


def transcribe_file(filename, data, scenario="extension", speaker="unknown", live=False, diarization_session_id=""):
    """오디오 → 확정 전사 목록. 자동 분리 모델이 없으면 받은 speaker를 그대로 쓴다."""
    maybe_fail("asr")
    duration = validate_audio(filename, data)
    if not config.REAL_MODELS:
        if live:
            return [{"speaker": speaker, "text": f"(Mock ASR) {duration or 0}초 음성 구간"}]
        return MOCK_SCRIPTS.get(scenario) or MOCK_SCRIPTS["extension"]
    r = post(config.ASR_BASE_URL.rstrip("/") + "/internal/asr/transcribe", "asr", "asr.transcribe",
             config.ASR_TIMEOUT_SECONDS if live else config.ASR_FILE_TIMEOUT_SECONDS,
             files={"file": (filename, data)}, data={"language": "한국어", "diarization_session_id": diarization_session_id})
    return [{"speaker": s.get("speaker") or speaker, "text": s["text"],
             "start": s.get("start"), "end": s.get("end")} for s in r["segments"] if s["text"].strip()]


# ---------- Decider ----------
# 시연 문장을 결정적으로 분류하기 위한 키워드. 실제 모델 판정이 아니다.
MOCK_KEYWORDS = {
    # 주제
    "보증 신청": ["신청하려", "신청하고 싶", "보증을 받고 싶", "자금이 필요", "운영비가", "시설자금", "대환"],
    "만기·연장": ["만기", "연장", "갱신"],
    "서류 문의": ["서류", "발급"],
    "진행상태 문의": ["신청했", "신청한거", "신청한 거", "접수했", "연락이 없", "진행 상황", "진행중인지", "진행 중인지", "심사"],
    "상환·연체": ["연체가", "연체됐", "연체 중", "상환을 못", "갚", "대위변제", "분할"],
    "보증료": ["보증료"],
    "보증 해지·변경": ["해지", "대표자 변경", "사업장 이전", "주소 변경", "폐업 예정", "폐업했"],
    "일반 안내·보안": ["사칭", "사기", "피싱", "영업시간", "지점", "본인확인", "컨설팅", "교육"],
    "신청 상담": ["신청하려", "신청하고 싶", "보증을 받고 싶", "자금이 필요", "운영비가", "시설자금", "대환", "운영하고"],
    "상품·한도 문의": ["특례", "정책자금", "한도"],
    "사칭·사기 의심": ["사칭", "사기", "피싱", "입금하라"], "본인확인": ["본인확인", "대리인"],
    "영업시간·지점": ["영업시간", "지점", "몇 시"], "경영지원": ["컨설팅", "교육"],
    "불만·민원": ["왜 이렇게", "왜이렇게", "늦어", "불친절", "민원", "화가", "답답"],
    # 보증 신청
    "운영 중": ["운영 중", "운영하고", "장사하고"], "창업 준비": ["창업"], "휴·폐업": ["휴업", "폐업"],
    "운전자금": ["운영비", "인건비", "재료비", "임차료", "운전자금"], "시설자금": ["시설", "장비"], "복합 용도": ["둘 다"],
    "대환": ["대환", "갈아타"],
    "긴급": ["급해", "급하게", "내일", "당장", "이번 주"], "일반": ["여유", "다음 달", "급하지 않"],
    "있음": ["연체가 있", "체납이 있", "연체 중"], "없음": ["연체 없", "체납 없", "연체는 없"],
    "등록 완료": ["사업자등록 했", "사업자 냈", "등록했"], "등록 전": ["등록 전", "아직 사업자"],
    # 만기·연장
    "만기 전": ["다가와", "남았", "만기 전"], "만기 경과": ["지났", "경과"],
    "전액 연장": ["연장하고 싶", "연장을 하고 싶", "연장 원"], "일부 상환 후 연장": ["일부 상환", "일부 갚"],
    "전액 상환": ["다 갚", "전액 상환", "완납"],
    "정상": ["정상 영업", "정상적으로", "연체 없"], "어려움 있음": ["매출이 줄", "휴업", "연체 중"],
    # 진행상태
    "신청했다고 진술": ["신청했", "신청한거", "신청한 거", "접수했", "제출했"], "아직 신청 전": ["아직 신청", "신청 전"],
    "상태 조회": ["진행상태", "어디까지", "진행 상황", "진행중인지", "진행 중인지"],
    "지연·재연락 요청": ["연락이 없", "늦어", "왜 이렇게", "왜이렇게", "된다더니", "다시 연락", "연락 주", "전화 주"],
    "결과·실행 문의": ["승인", "거절", "통보", "대출 실행"],
    "임박": ["급해", "급하게", "내일", "당장", "이번 주"], "여유 있음": ["여유", "급하지 않", "천천히"],
    "대출 실행": ["대출 실행", "입금", "은행에 언제"], "거절·감액 사유": ["거절", "감액", "사유"],
    # 서류
    "목록 확인": ["어떤 서류", "무슨 서류", "필요한 서류", "서류 목록", "서류가 필요"], "발급 방법": ["발급", "떼려"],
    "제출 방법": ["제출", "업로드", "올리"], "보완 요청": ["보완"],
    "신규 신청": ["신청하려", "신규"], "기한연장": ["연장"],
    # 상환·연체
    "정상 상환 중": ["잘 갚", "정상 상환", "꼬박꼬박"], "연체 중": ["연체가", "연체됐", "연체 중", "상환을 못"], "대위변제 통지": ["대위변제", "대신 갚"], "상환 완료": ["다 갚았", "상환 완료", "완납했"],
    "중도상환": ["중도상환", "미리 갚"], "일정·잔액": ["잔액", "얼마 남", "상환 일정"],
    "분할·유예": ["분할", "나눠", "유예", "미뤄"], "채무조정": ["채무조정", "새출발", "감면"],
    "연체 사실 확인": ["연체됐는지", "연체 확인", "연체 금액"],
    "구상채권 상환": ["구상", "갚아야"], "신용 회복": ["신용", "기록"],
    # 보증료·해지·민원
    "금액·요율": ["얼마", "요율"], "납부 방법": ["납부", "내는 방법", "계좌"], "환급": ["환급", "돌려"],
    "보증 해지": ["해지"], "정보 변경": ["대표자", "주소", "사업장 이전", "변경"], "폐업": ["폐업"],
    "처리 지연": ["늦어", "지연", "연락이 없", "왜 이렇게", "왜이렇게"], "직원 응대": ["불친절", "응대", "직원"],
    "심사 결과": ["거절", "한도", "왜 안"],
    # 감정
    "분노": ["화가", "짜증", "어이없"], "불만": ["불편", "실망", "답답", "늦어"], "불안": ["걱정", "불안"],
    "만족/기쁨": ["감사", "고맙"], "슬픔": ["속상", "힘들"],
}
INTENSITY = {"분노": "3", "불만": "2", "슬픔": "2", "불안": "1"}


def _hits(label, text):
    return sum(k in text for k in MOCK_KEYWORDS.get(label, [label]))


def _choice(scores, fallback):
    best = max(scores, key=scores.get)
    label = best if scores[best] > 0 else fallback
    rest = [k for k in scores if k != label]
    probs = {k: round(0.14 / len(rest), 4) for k in rest}
    probs[label] = 0.86
    return {"type": "choice", "choice": label, "probabilities": probs}


class MockDecider:
    """app.Runtime.decide와 같은 시그니처·응답 형식. run_routing에 그대로 넣을 수 있다."""
    settings = None

    def decide(self, state, questions, images, layout, temperature):
        maybe_fail("decider")
        text = state.get("current_transcript") or state.get("confirmed_consultation_transcript", "")
        customer = [l for l in text.splitlines() if "[고객]" in l]  # 고객 진술만 사실로 사용
        text = "\n".join(customer) or text
        answers = {}
        for key, q in questions.items():
            if q["type"] == "score":
                emotion = answers.get("emotion", {}).get("choice", "중립")
                legend = {str(i): c.split(" ", 1)[1] for i, c in enumerate(q["criteria"])}
                level = INTENSITY.get(emotion, "0")
                probs = {k: (0.8 if k == level else round(0.2 / (len(legend) - 1), 4)) for k in legend}
                answers[key] = {"type": "score", "score": round(sum(int(k) * p for k, p in probs.items()), 3),
                                "probabilities": probs, "legend": legend}
            elif "해당" in q["criteria"]:  # 주제 해당 여부 질문
                topic = q["instructions"].rsplit("포함되는가? ", 1)[-1].split(":")[0]
                answers[key] = _choice({"해당": _hits(topic, text), "해당 없음": 0, "확인 필요": 0}, "해당 없음")
            else:
                scores = {label: _hits(label, text) for label in q["criteria"]}
                fallback = "확인 필요" if "확인 필요" in scores else ("중립" if "중립" in scores else next(iter(scores)))
                answers[key] = _choice(scores, fallback)
        return {"answers": answers, "usage": {"input_tokens": 0, "output_tokens": 0, "sequences": 0}}, 0.0


class HttpDecider:
    """model_server.py /internal/decider/evaluate 호출. 응답 형식은 app.Runtime.decide와 동일."""
    settings = None

    def decide(self, state, questions, images, layout, temperature):
        maybe_fail("decider")
        r = post(config.DECIDER_BASE_URL.rstrip("/") + "/internal/decider/evaluate", "decider", "decider.evaluate",
                 config.DECIDER_TIMEOUT_SECONDS, json={"state": state, "questions": questions, "layout": layout})
        for key, q in questions.items():  # 스키마 검증: 요청한 질문 모두에 답했는지
            if r["answers"][key]["type"] not in ("choice", "score", "noul"):
                raise ValueError("decider answer type")
        return {"answers": r["answers"], "usage": r.get("usage", {})}, r.get("seconds", 0.0)


decider = HttpDecider() if config.REAL_MODELS else MockDecider()
ROUTING_CONFIG = legacy.validate_routing_config(json.loads(config.ROUTING_CONFIG_PATH.read_text(encoding="utf-8")))
EMOTION_QUESTIONS = legacy.EMOTION_QUESTIONS


def evaluate_emotion(text):
    result, _ = decider.decide({"current_transcript": text[-1600:]}, EMOTION_QUESTIONS, [], "공동 판정 (joint)", None)
    return result


def evaluate_routing(transcript):
    return legacy.run_routing(decider, transcript, ROUTING_CONFIG)


# ---------- Knowledge ----------
def _load_docs():
    docs = []
    for path in sorted(config.KNOWLEDGE_PATH.glob("*.json")):
        docs += json.loads(path.read_text(encoding="utf-8"))
    return docs


DOCS = _load_docs()


def search_knowledge(text, topics):
    """키워드/주제 일치 검색. 결과 없으면 빈 목록(존재하지 않는 문서를 만들지 않는다)."""
    maybe_fail("knowledge")
    if config.KNOWLEDGE_MODE == "qdrant":
        from . import knowledge
        return knowledge.search(text, topics)
    # ponytail: 키워드 매칭. 업무지침 RAG가 생기면 이 함수만 교체
    scored = [(sum(k in text for k in d["keywords"]) + 2 * bool(set(d["topics"]) & set(topics)), d) for d in DOCS]
    return [{k: d[k] for k in ("document_id", "title", "section", "snippet", "source")}
            for s, d in sorted(scored, key=lambda x: -x[0]) if s > 0][:3]


# ---------- Coach ----------
def coach_model():
    return {"mock": "mock-coach", "api": config.COACH_MODEL_NAME, "internal": "Qwen/Qwen3.5-2B (NF4)"}.get(config.COACH_MODE)


def ground_terminal_guidance(guidance, routing):
    """후속 업무가 확정된 뒤에는 모델 메모가 고객 진술을 바꾸지 못하게 한다."""
    paths = routing.get("paths") or []
    work = [p for p in paths if p.get("topic") != "불만·민원"] or paths
    path = next((p for p in work if p.get("action")), None)
    if not path:
        return guidance
    facts = ", ".join(f"{s['title']}: {s['choice']}" for s in path.get("steps", []) if s.get("certain"))
    note = f"{path['topic']} 상담입니다." + (f" 확인된 내용: {facts}." if facts else "") + " 업무시스템 확인 후 안내가 필요합니다."
    return legacy.parse_guidance(json.dumps({"staff_note": note[:220], "customer_question": "",
                                             "action_items": [path["action"][:140]]}, ensure_ascii=False))


def generate_guidance(context, routing):
    """기존 app.py 출력 스키마(staff_note/customer_question/action_items)를 parse_guidance로 검증."""
    maybe_fail("coach")
    if config.COACH_MODE == "mock":
        path = (routing.get("paths") or [{}])[0]
        note = (f"{path['topic']} 상담으로 판단됨 · {path['status']}" if path
                else "상담 목적이 아직 확인되지 않았습니다.")
        question = path.get("next_question") or routing.get("next_question") or ""
        text = json.dumps({"staff_note": note, "customer_question": question,
                           "action_items": [p["action"][:140] for p in routing.get("paths", []) if p.get("action")][:2]},
                          ensure_ascii=False)
        return ground_terminal_guidance(legacy.parse_guidance(text), routing)
    if config.COACH_MODE == "internal":
        r = post(config.COACH_BASE_URL.rstrip("/") + "/internal/coach/generate", "coach", "coach.http",
                 config.COACH_TIMEOUT_SECONDS, json={"context": context})
        return ground_terminal_guidance(legacy.parse_guidance(json.dumps(r["guidance"], ensure_ascii=False)), routing)  # 서버 응답도 다시 검증
    if config.COACH_MODE != "api":
        raise AppError("UPSTREAM_UNAVAILABLE", "Coach가 꺼져 있습니다.", 503)
    import os
    body = {"model": config.COACH_MODEL_NAME, "stream": False, "max_tokens": 320, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": legacy.COACH_SYSTEM},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
    headers = {"Authorization": "Bearer " + os.environ["COACH_API_KEY"]} if os.environ.get("COACH_API_KEY") else {}
    r = post(config.COACH_BASE_URL.rstrip("/") + "/chat/completions", "coach", "coach.http",
             config.COACH_TIMEOUT_SECONDS, json=body, headers=headers)
    choice = r["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("LLM 안내가 출력 한도로 잘렸습니다.")
    return ground_terminal_guidance(legacy.parse_guidance(choice["message"]["content"]), routing)


SPEAKER_KO = {"customer": "고객", "agent": "상담원", "unknown": "미구분"}


def routing_transcript(rows):
    return "\n".join(f"[{SPEAKER_KO.get(r.speaker, '미구분')}] {re.sub(r'\s+', ' ', r.text)}" for r in rows)
