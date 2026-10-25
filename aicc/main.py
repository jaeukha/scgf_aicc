"""AICC 백엔드: 세션 오케스트레이션, WebSocket, 모니터링, 정적 UI."""
import asyncio
import contextvars
import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, text as sql
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config, crm, providers as P, twenty_sync
from .db import (AIAnalysisResult, ApiCallTrace, CoachSuggestion, FollowUpTask, ConsultationNote,
                 ConsultationSession, Session, TranscriptEvent, now, seed, utc)
from .telemetry import SERVICES, AppError, classify, new_trace, percentile, recent, service_status, session_var, span, summarize, trace_var

logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(_):
    with Session() as db:  # 스키마는 alembic upgrade head로 생성. 여기서는 가상 시드만
        seed(db)
    yield


app = FastAPI(title="AICC 상담원 보조 시연", lifespan=lifespan)
if config.CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


UPSTREAM_ERRORS = {"UPSTREAM_TIMEOUT": (504, "상위 서비스 응답 시간이 초과되었습니다."),
                   "UPSTREAM_UNAVAILABLE": (503, "상위 서비스에 연결할 수 없습니다."),
                   "UPSTREAM_BAD_RESPONSE": (502, "상위 서비스가 잘못된 응답을 반환했습니다."),
                   "SCHEMA_VALIDATION_FAILED": (502, "상위 서비스 응답 형식 검증에 실패했습니다."),
                   "RATE_LIMITED": (429, "요청이 너무 많습니다."),
                   "AUDIO_FORMAT_UNSUPPORTED": (415, "지원하지 않는 오디오 형식입니다.")}


def error_body(code, message, status):
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}, "trace_id": trace_var.get()},
                        status_code=status)


@app.middleware("http")
async def trace_requests(request: Request, call_next):
    tid = request.headers.get("x-trace-id", "")
    trace_var.set(tid) if re.fullmatch(r"[0-9a-f-]{36}", tid) else new_trace()
    path = request.url.path
    # 업로드 본문을 다 받기 전에 크기 초과를 거절한다(Starlette는 multipart를 끝까지 받은 뒤 핸들러를 부름)
    if path.endswith(("/audio", "/audio-segment")) and int(request.headers.get("content-length") or 0) > config.AUDIO_MAX_BYTES + 1024 * 1024:
        response = error_body("AUDIO_TOO_LARGE", "오디오 파일이 너무 큽니다.", 413)
        response.headers["X-Trace-Id"] = trace_var.get()
        return response
    # 모니터링 폴링은 자기 자신을 기록하지 않는다
    traced = path.startswith("/api/") and not path.startswith("/api/v1/monitoring") and not path.endswith("/stream")
    if not traced:
        response = await call_next(request)
    else:
        # ponytail: span DB 기록이 이벤트 루프에서 동기 실행됨. 부하가 생기면 큐+배치 기록으로
        route = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{id}", path)
        try:
            with span("backend", f"{request.method} {route}", call_kind="http") as extra:
                response = await call_next(request)
                extra["http_status_code"] = response.status_code
                if response.status_code >= 500:
                    extra["status"], extra["error_code"] = "error", "INTERNAL_ERROR"
        except Exception as exc:  # 처리되지 않은 상위 서비스 오류도 표준 코드로 응답
            logging.getLogger("aicc").exception("unhandled error trace_id=%s", trace_var.get())  # 본문·전사는 남기지 않음
            _, code, _ = classify(exc)
            http, msg = UPSTREAM_ERRORS.get(code, (500, "내부 오류가 발생했습니다."))
            response = error_body(code, msg, http)
    response.headers["X-Trace-Id"] = trace_var.get()
    return response


@app.exception_handler(AppError)
async def app_error(_, exc: AppError):
    return error_body(exc.code, exc.message, exc.http_status)


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc):
    return error_body("SCHEMA_VALIDATION_FAILED", "요청 형식이 올바르지 않습니다.", 422)


@app.exception_handler(IntegrityError)
async def integrity_error(_, exc):
    # 없는 직원·고객 ID, 필수 값 null 등 DB 무결성 위반은 모든 쓰기 API에서 같은 422로 응답
    return error_body("INVALID_REFERENCE", "존재하지 않는 대상을 가리키거나 필수 값이 비어 있습니다.", 422)


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc):
    return error_body("NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR", "요청한 경로를 처리할 수 없습니다.", exc.status_code)


# ---------- 오케스트레이션 ----------
_locks: dict[str, threading.Lock] = {}
_generation: dict[str, int] = {}


def get_session(db, sid) -> ConsultationSession:
    s = db.get(ConsultationSession, sid)
    if s is None:
        raise AppError("SESSION_NOT_FOUND", "상담 세션을 찾을 수 없습니다.", 404)
    return s


def safe(step):
    """한 단계 실패가 다음 단계를 막지 않게 한다. 오류는 span에 기록된다."""
    try:
        return step()
    except Exception:
        return None


PII_PATTERNS = [  # 시연은 가상 데이터만 쓰지만, 실수로 말한 식별정보가 DB·화면·CRM에 남지 않게 가린다
    (re.compile(r"\d{6}\s*-\s*[1-8]\d{6}"), "[주민등록번호]"),
    (re.compile(r"\d{3}\s*-\s*\d{2}\s*-\s*\d{5}"), "[사업자등록번호]"),
    (re.compile(r"01[016789]\s*-?\s*\d{3,4}\s*-?\s*\d{4}"), "[전화번호]"),
    (re.compile(r"\d{4}\s*-\s*\d{4}\s*-\s*\d{4}\s*-\s*\d{4}"), "[카드번호]"),
    (re.compile(r"\b\d{2,6}-\d{2,6}-\d{4,8}(?:-\d{1,6})?\b"), "[계좌번호]"),  # 마지막 묶음 4자리 이상: 날짜 제외
]


def mask_pii(text):
    for pattern, label in PII_PATTERNS:
        text = pattern.sub(label, text)
    return text


def process_utterance(sid: uuid.UUID, speaker: str, text: str, source: str, faults: dict | None = None):
    """확정 발화 → 저장 → 감정 → 상담 라우팅 → 지식 검색 → Coach. 결과는 DB 이벤트로 남는다."""
    P.fault_var.set(faults or {})
    session_var.set(sid)
    text = mask_pii(text)
    key = str(sid)
    with _locks.setdefault(key, threading.Lock()):  # 확정 발화 순서 보장
        with Session() as db:
            s = get_session(db, sid)
            if s.status != "active":
                raise AppError("SESSION_NOT_ACTIVE", "진행 중인 세션이 아닙니다.", 409)
            seq = (db.query(func.max(TranscriptEvent.sequence_no)).filter_by(session_id=sid).scalar() or 0) + 1
            with span("db", "transcript.insert", session_id=sid):
                ev = TranscriptEvent(session_id=sid, sequence_no=seq, speaker=speaker, text=text,
                                     source=source, trace_id=trace_var.get())
                db.add(ev)
                db.commit()
            gen = _generation[key] = _generation.get(key, 0) + 1

            def emotion():
                with span("decider", "decider.emotion", session_id=sid, provider=P.MODE,
                          model_version=config.MODEL_VERSIONS["decider"]):
                    r = P.evaluate_emotion(text)
                db.add(AIAnalysisResult(session_id=sid, source_event_id=ev.event_id, analysis_type="sentiment_text",
                                        result_json=r, model_name=f"{P.MODE}-decider", needs_human_review=False))
                db.commit()

            if speaker != "agent":  # 텍스트 기반 감정 추정은 고객/미구분 발화만
                safe(emotion)

            rows = db.query(TranscriptEvent).filter_by(session_id=sid).order_by(
                TranscriptEvent.sequence_no.desc()).limit(config.ROUTING_WINDOW).all()[::-1]
            transcript = P.routing_transcript(rows)

            def routing():
                with span("decider", "decider.routing", session_id=sid, provider=P.MODE,
                          model_version=config.MODEL_VERSIONS["decider"]):
                    r = P.evaluate_routing(transcript)
                paths = r.get("paths", [])
                first = paths[0] if paths else {}
                stage = first.get("destination") or (first["steps"][-1]["title"] if first.get("steps") else None)
                normalized = {
                    "topic": first.get("topic"), "stage": stage,
                    "next_step": first.get("action") or first.get("next_question") or r.get("next_question"),
                    "questions": [q for q in [p.get("next_question") for p in paths] + [r.get("next_question")] if q],
                    "required_checks": [p["action"] for p in paths if p.get("action")],
                    "confidence": None,  # Decider 선택 확률은 정확도 지표가 아니므로 confidence로 쓰지 않는다
                    "needs_human_review": not paths or any(p["status"] == "추가 확인 필요" for p in paths),
                    "reasoning_summary": r["status"]}
                stored = {k: v for k, v in r.items() if k not in ("trace", "source_text")}  # 전사 원문 중복 저장 안 함
                db.add(AIAnalysisResult(session_id=sid, source_event_id=ev.event_id, analysis_type="routing",
                                        topic=normalized["topic"], stage=stage, next_step=normalized["next_step"],
                                        needs_human_review=normalized["needs_human_review"],
                                        result_json={**stored, "normalized": normalized}, model_name=f"{P.MODE}-decider"))
                s.current_topic, s.current_stage = normalized["topic"], stage
                db.commit()
                return r

            route = safe(routing) or {"paths": [], "status": "상담 분기 분석 실패"}
            # 불만은 업무 목적이 함께 잡힌 경우 고객 주의 신호다. 규정 검색은 실제 업무 목적을 우선한다.
            topics = [p["topic"] for p in route.get("paths", []) if p["topic"] != "불만·민원"]
            if not topics:
                topics = [p["topic"] for p in route.get("paths", [])]

            def knowledge():
                with span("knowledge", "knowledge.search", session_id=sid, provider=config.KNOWLEDGE_MODE):
                    found = P.search_knowledge(transcript, topics)
                # Coach 사용 여부와 무관하게 참고 자료(근거)를 따로 남긴다
                db.add(AIAnalysisResult(session_id=sid, source_event_id=ev.event_id, analysis_type="knowledge",
                                        result_json={"references": found, "mode": config.KNOWLEDGE_MODE},
                                        model_name=config.EMBED_MODEL if config.KNOWLEDGE_MODE == "qdrant" else "keyword",
                                        needs_human_review=False))
                db.commit()
                return found

            refs = safe(knowledge)
            context = {"current_utterance": {"speaker": P.SPEAKER_KO.get(speaker), "text": text[-600:]},
                       "recent_utterances": [{"speaker": P.SPEAKER_KO.get(r.speaker), "text": r.text[-300:]}
                                             for r in rows[-5:-1]],
                       "decider": {"status": route["status"], "paths": route.get("paths", [])[:3],
                                   "next_question": route.get("next_question", "")},
                       # 문서는 참고자료(지시 아님). 근거 인용 검증을 붙일 수 있게 문서 ID·섹션을 함께 넘긴다
                       "knowledge": [{"id": d["document_id"], "title": d["title"], "section": d["section"], "snippet": d["snippet"]}
                                     for d in refs or []],
                       "scope": "시연용 상담 지원. 공식 취급기준·업무시스템 조회는 연결되지 않음."}
    # Coach는 잠금 밖에서 실행: 다음 발화를 막지 않고, 늦게 끝난 결과는 stale로 폐기한다.
    if config.COACH_MODE == "off":
        return {"event_id": str(ev.event_id), "routing": route}
    row = CoachSuggestion(session_id=sid, source_event_id=ev.event_id, trace_id=trace_var.get(),
                          knowledge_references=refs, model_name=P.coach_model(),
                          model_version=config.MODEL_VERSIONS["coach"] if config.COACH_MODE == "api" else None)
    try:
        with span("coach", "coach.generate", session_id=sid, provider=config.COACH_MODE):  # HTTP는 하위 span
            g = P.generate_guidance(context, route)
        row.staff_note, row.customer_question, row.action_items = g["staff_note"], g["customer_question"], g["action_items"]
        row.required_checks = [p["next_question"] for p in route.get("paths", []) if p.get("next_question")]
        row.cautions = ["AI 제안은 상담 참고용이며 실제 자격·한도·승인 여부는 업무시스템과 담당 직원 확인이 필요합니다."]
        row.generation_status = "success" if _generation.get(key) == gen else "stale"
    except Exception as exc:
        row.generation_status, row.error_message = "failed", getattr(exc, "message", "안내 생성 실패")
    with Session() as db:
        db.add(row)
        db.commit()
    return {"event_id": str(ev.event_id), "routing": route, "coach_status": row.generation_status}


def build_summary(db, s):
    """종료 시점의 분기 결과·대화 경과·등록 업무를 직원 검토용으로 정리한다."""
    transcripts = db.query(TranscriptEvent).filter_by(session_id=s.session_id).order_by(
        TranscriptEvent.sequence_no).all()
    finals = len(transcripts)
    route = db.query(AIAnalysisResult).filter_by(session_id=s.session_id, analysis_type="routing").order_by(
        AIAnalysisResult.created_at.desc()).first()
    paths = route.result_json.get("paths", []) if route else []
    work_paths = [p for p in paths if p.get("topic") != "불만·민원"] or paths
    complaint_paths = [p for p in paths if p.get("topic") == "불만·민원"] if work_paths != paths else []
    feelings = [a.result_json["answers"] for a in db.query(AIAnalysisResult).filter_by(
        session_id=s.session_id, analysis_type="sentiment_text")]
    peak = max((f["negative_intensity"]["score"] for f in feelings), default=0)
    tasks = db.query(FollowUpTask).filter_by(session_id=s.session_id).all()
    # shortcut: 모델 자유요약은 사실을 바꿀 수 있어, 검증된 상담종료 요약 모델 평가 전까지 확정 데이터만 사용
    lines = ["[AI 생성 요약·직원 확인 필요]", "## 상담 목적"]
    lines.extend(f"- {p['topic']}" for p in work_paths) if work_paths else lines.append("- 상담 목적이 확인되지 않았습니다.")
    lines.append("## 대화에서 확인된 사실")
    facts = [f"{p['topic']}: " + ", ".join(f"{st['title']} {st['choice']}" for st in p.get("steps", [])
                                                if st.get("certain") and st.get("choice") != "확인 필요")
             for p in work_paths if any(st.get("certain") and st.get("choice") != "확인 필요" for st in p.get("steps", []))]
    lines.extend(f"- {fact}" for fact in facts) if facts else lines.append("- 확정된 확인 항목이 없습니다.")
    lines.extend(f"- 고객 불만 신호: {p['destination']}" for p in complaint_paths if p.get("destination"))
    lines.append("## 상담 경과")
    customer_count = sum(t.speaker == "customer" for t in transcripts)
    staff_count = sum(t.speaker == "agent" for t in transcripts)
    lines.append(f"- 대화 {finals}문장 (고객 {customer_count}, 상담원 {staff_count})")
    lines.append(f"- 고객 감정 신호: 부정 표현 {'강함' if peak >= 2.5 else '보통' if peak >= 1.5 else '약함 또는 없음'}")
    lines.append("## 미확인 항목")
    open_items = [f"{p['topic']}: {p['next_question']}" for p in work_paths if p.get("next_question")]
    lines.extend(f"- {item}" for item in open_items) if open_items else lines.append("- 현재 분기에서 추가 확인 항목이 없습니다.")
    lines.append("## 후속 업무")
    lines.extend(f"- {t.title} ({t.status})" for t in tasks) if tasks else lines.append("- 등록한 후속 업무가 없습니다.")
    return "\n".join(lines)


def session_events(db, sid):
    """세션 시작/전사/분석/안내/메모/오류/감사를 공통 이벤트 형식으로 합친다."""
    iso = lambda t: (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()
    s = get_session(db, sid)
    ev = [{"id": f"s:{sid}", "type": "session_started", "at": iso(s.started_at), "data": {"input_mode": s.input_mode}}]
    if s.ended_at:
        ev.append({"id": f"e:{sid}", "type": "session_ended", "at": iso(s.ended_at), "data": {"status": s.status}})
    for t in db.query(TranscriptEvent).filter_by(session_id=sid):
        ev.append({"id": str(t.event_id), "type": "transcript", "at": iso(t.occurred_at),
                   "data": {"seq": t.sequence_no, "speaker": t.speaker, "text": t.text,
                            "transcript_type": t.transcript_type, "source": t.source}})
    for a in db.query(AIAnalysisResult).filter_by(session_id=sid):
        ev.append({"id": str(a.analysis_id), "type": f"analysis.{a.analysis_type}", "at": iso(a.created_at),
                   "data": {"source_event_id": str(a.source_event_id), "topic": a.topic, "stage": a.stage,
                            "next_step": a.next_step, "needs_human_review": a.needs_human_review,
                            "result": a.result_json, "model": a.model_name}})
    for c in db.query(CoachSuggestion).filter_by(session_id=sid):
        ev.append({"id": str(c.suggestion_id), "type": "coach", "at": iso(c.created_at),
                   "data": {k: getattr(c, k) for k in ("staff_note", "customer_question", "action_items",
                            "required_checks", "cautions", "knowledge_references", "generation_status",
                            "error_message", "model_name")} | {"source_event_id": str(c.source_event_id)}})
    for n in db.query(ConsultationNote).filter_by(session_id=sid, is_deleted=False):
        ev.append({"id": str(n.note_id), "type": "note", "at": iso(n.created_at),
                   "data": {"text": n.note_text, "author": str(n.author_employee_id)}})
    for e in db.query(ApiCallTrace).filter(ApiCallTrace.session_id == str(sid), ApiCallTrace.status != "success"):
        ev.append({"id": e.span_id, "type": "error", "at": iso(e.started_at),
                   "data": {"service": e.service_name, "operation": e.operation, "error_code": e.error_code,
                            "message": e.error_message, "trace_id": e.trace_id}})
    return sorted(ev, key=lambda e: e["at"])


# ---------- 세션 API ----------
class SessionCreate(crm.Strict):
    input_mode: str = Field("simulation", pattern="^(file|microphone|simulation)$")
    customer_id: uuid.UUID | None = None
    assigned_employee_id: uuid.UUID | None = None


class Utterance(crm.Strict):
    text: str = Field(min_length=1, max_length=2000)
    speaker: str = Field("unknown", pattern="^(customer|agent|unknown)$")
    inject_error: dict[str, str] | None = None  # 시연용 {"coach": "timeout|unavailable|bad_response"}, APP_ENV=local에서만


def session_dict(s):
    return {k: (str(v) if isinstance(v, uuid.UUID) else utc(v).isoformat() if isinstance(v, datetime) else v)
            for k, v in s.__dict__.items() if not k.startswith("_")}


@app.get("/api/v1/meta")
def meta():
    return {"app_env": config.APP_ENV, "provider_mode": config.PROVIDER_MODE, "coach_mode": config.COACH_MODE,
            "asr": P.MODE, "decider": P.MODE, "knowledge": config.KNOWLEDGE_MODE, "routing_version": P.ROUTING_CONFIG["version"],
            "scenarios": list(P.MOCK_SCRIPTS), "scripts": P.MOCK_SCRIPTS,
            "topics": [t["title"] for t in P.ROUTING_CONFIG["topics"]], "model_versions": config.MODEL_VERSIONS}


@app.post("/api/v1/sessions")
def create_session(body: SessionCreate):
    with Session() as db:
        s = ConsultationSession(**body.model_dump())
        db.add(s)
        db.commit()
        return {"session_id": str(s.session_id), "created_at": s.created_at.isoformat(), "status": s.status}


@app.get("/api/v1/sessions/{sid}")
def read_session(sid: uuid.UUID):
    with Session() as db:
        return session_dict(get_session(db, sid))


@app.post("/api/v1/sessions/{sid}/simulate")
def simulate(sid: uuid.UUID, body: Utterance):
    faults = body.inject_error if config.APP_ENV == "local" else None
    return process_utterance(sid, body.speaker, body.text, "simulation", faults)


_replaying: set[str] = set()  # 세션당 파일 재생은 하나만(두 파일 전사가 섞이지 않게)
PAUSE_LIMIT_SECONDS = 30 * 60


def replay(sid, filename, data, scenario, speaker):
    """업로드 파일: 실제 ASR 시각에 맞춰 확정 발화를 순서대로 처리한다."""
    session_var.set(sid)
    replay_started = time.monotonic()
    try:
        with span("asr", "asr.transcribe_file", provider=P.MODE, model_version=config.MODEL_VERSIONS["asr"]) as extra:
            extra["request_size_bytes"] = len(data)
            lines = P.transcribe_file(filename, data, scenario, speaker, diarization_session_id=str(sid))
        del data  # 원본 음성을 전사 직후 메모리에서 놓는다
        for line in lines:
            waited = 0.0
            while True:  # 일시정지 중이면 대기(최대 30분), 종료되면 중단
                with Session() as db:
                    status = get_session(db, sid).status
                if status != "paused" or waited >= PAUSE_LIMIT_SECONDS:
                    break
                time.sleep(0.5)
                waited += 0.5
            if status != "active":
                return
            if config.REAL_MODELS and line.get("start") is not None:
                remaining = float(line["start"]) - (time.monotonic() - replay_started)
                if remaining > 0:
                    time.sleep(remaining)
            new_trace()
            safe(lambda: process_utterance(sid, line["speaker"], line["text"], "asr"))
            if not config.REAL_MODELS:
                time.sleep(config.MOCK_DELAY_SECONDS)  # mock 전사를 실시간처럼 보이게
    except Exception:
        return  # 오류는 span → 세션 오류 이벤트로 표시됨
    finally:
        _replaying.discard(str(sid))


SpeakerRole = Query("unknown", pattern="^(customer|agent|unknown)$")  # 사용자 지정 역할, 자동 화자 분리 아님


@app.post("/api/v1/sessions/{sid}/audio")
async def upload_audio(sid: uuid.UUID, file: UploadFile = File(...),
                       scenario: str = "extension", speaker: str = SpeakerRole):
    data = await file.read(config.AUDIO_MAX_BYTES + 1)  # 원본은 메모리에서만 처리, 저장하지 않음

    def check():
        with Session() as db:
            if get_session(db, sid).status != "active":
                raise AppError("SESSION_NOT_ACTIVE", "진행 중인 세션이 아닙니다.", 409)
        if str(sid) in _replaying:
            raise AppError("AUDIO_IN_PROGRESS", "이 상담에서 이미 음성 파일을 분석하고 있습니다. 끝난 뒤 다시 올리세요.", 409)
        return P.validate_audio(file.filename, data)

    duration = await run_in_threadpool(check)
    _replaying.add(str(sid))
    # 공용 스레드풀(BackgroundTasks)을 장시간 점유하지 않도록 전용 스레드에서 처리
    threading.Thread(target=contextvars.copy_context().run, args=(replay, sid, file.filename, data, scenario, speaker),
                     daemon=True).start()
    return {"accepted": True, "audio_seconds": duration, "asr": P.MODE}


@app.post("/api/v1/sessions/{sid}/audio-segment")
def audio_segment(sid: uuid.UUID, file: UploadFile = File(...), speaker: str = SpeakerRole):
    """브라우저 마이크가 무음 기준으로 자른 한 구간(WAV). 전사 후 확정 발화로 처리."""
    session_var.set(sid)
    data = file.file.read(config.AUDIO_MAX_BYTES + 1)
    with Session() as db:
        if get_session(db, sid).status != "active":
            raise AppError("SESSION_NOT_ACTIVE", "진행 중인 세션이 아닙니다.", 409)
    with span("asr", "asr.transcribe_segment", provider=P.MODE, model_version=config.MODEL_VERSIONS["asr"]):
        lines = P.transcribe_file(file.filename or "segment.wav", data, speaker=speaker, live=True,
                                  diarization_session_id=str(sid))
    return [process_utterance(sid, l["speaker"], l["text"], "asr") for l in lines]


def set_status(sid, allowed, new):
    with Session() as db:
        s = get_session(db, sid)
        if s.status not in allowed:
            raise AppError("INVALID_STATE", f"현재 상태({s.status})에서는 변경할 수 없습니다.", 409)
        s.status = new
        db.commit()
        return session_dict(s)


@app.post("/api/v1/sessions/{sid}/pause")
def pause(sid: uuid.UUID):
    return set_status(sid, ("active",), "paused")


@app.post("/api/v1/sessions/{sid}/resume")
def resume(sid: uuid.UUID):
    return set_status(sid, ("paused",), "active")


@app.post("/api/v1/sessions/{sid}/end")
def end_session(sid: uuid.UUID):
    key = str(sid)
    # 처리 중인 발화(저장·분석)가 끝난 뒤 종료해 마지막 발화가 요약에서 빠지지 않게 한다
    with _locks.setdefault(key, threading.Lock()), Session() as db:
        s = get_session(db, sid)
        if s.status in ("completed", "failed"):
            return session_dict(s)
        s.status, s.ended_at = "completed", now()
        db.commit()  # 요약 실패가 종료를 막지 않도록 먼저 커밋
        try:
            with span("coach", "coach.summarize", session_id=sid, provider=config.COACH_MODE):
                s.ai_summary = build_summary(db, s)
            db.commit()
        except Exception:
            db.rollback()
        twenty_sync.schedule(sid)
        result = session_dict(s)
    _locks.pop(key, None)  # 종료된 세션은 더 처리하지 않으므로 세션별 상태 정리
    _generation.pop(key, None)
    return result


@app.get("/api/v1/sessions/{sid}/events")
def events(sid: uuid.UUID):
    with Session() as db:
        return session_events(db, sid)


@app.websocket("/api/v1/sessions/{sid}/stream")
async def stream(ws: WebSocket, sid: uuid.UUID):
    # ponytail: 인증 없음(시연은 내부망 전제). 최소한 다른 출처의 페이지가 브라우저로 엿듣지 못하게 Origin을 확인
    origin = ws.headers.get("origin")
    if origin and urlsplit(origin).netloc != ws.headers.get("host") and origin not in config.CORS_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    seen = set()
    try:
        while True:
            # ponytail: DB 폴링 기반 push(0.5초). 세션 이벤트가 수천 건이 되면 pub/sub로 교체
            def load():
                with Session() as db:
                    return session_events(db, sid), get_session(db, sid).status
            evs, status = await run_in_threadpool(load)
            for e in evs:
                if e["id"] not in seen:
                    seen.add(e["id"])
                    await ws.send_json(e)
            await ws.send_json({"type": "heartbeat", "status": status})
            if status in ("completed", "failed"):  # 종료된 상담은 더 바뀌지 않으므로 폴링을 끝낸다
                await ws.close()
                return
            await asyncio.sleep(0.5)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except AppError as exc:
        await ws.send_json({"type": "error", "data": {"error_code": exc.code, "message": exc.message}})
        await ws.close()


# ---------- 모니터링 ----------
@app.get("/health/live")
def live():
    return {"status": "ok"}


def db_ok():
    try:
        with Session() as db:
            db.execute(sql("select 1"))
        return True
    except Exception:
        return False


@app.get("/health/ready")
def ready():
    ok = db_ok()
    return JSONResponse({"status": "ready" if ok else "not_ready", "db": ok}, status_code=200 if ok else 503)


def services(db):
    rows = recent(db, 5)
    # ponytail: 조회 시점 healthcheck. 주기적 백그라운드 점검이 필요해지면 루프로 이동
    urls = {"asr": config.ASR_BASE_URL, "decider": config.DECIDER_BASE_URL} if config.REAL_MODELS else {}
    if config.COACH_MODE == "internal":
        urls["coach"] = config.COACH_BASE_URL
    probes = {url: P.probe(url) for url in set(urls.values())}
    out = []
    for name in SERVICES:
        mine = [r for r in rows if r.service_name == name]
        status = service_status(mine)
        if name == "db":
            status = "healthy" if db_ok() and status != "unavailable" else "unavailable"
        if name in urls:
            health = probes[urls[name]]
            if health is None or not health.get(name):  # 서버 다운 또는 모델 미로드
                status = "unavailable"
            elif status == "unknown":
                status = "healthy"
        if name == "knowledge" and config.KNOWLEDGE_MODE == "qdrant":
            from . import knowledge
            status = "unavailable" if not knowledge.healthy() else "healthy" if status == "unknown" else status
        mode = {"asr": P.MODE, "decider": P.MODE, "coach": config.COACH_MODE, "knowledge": config.KNOWLEDGE_MODE,
                "crm": "twenty" if config.TWENTY_ENABLED else "off"}.get(name)
        if name == "crm" and config.TWENTY_ENABLED and status == "unknown":
            status = "healthy" if twenty_sync.healthy() else "unavailable"
        if name == "coach" and config.COACH_MODE == "off":
            status = "unknown"
        out.append({"service": name, "status": status, "mode": mode,
                    "last_checked_at": mine[0].started_at.isoformat() if mine else None})
    return out


@app.get("/api/v1/monitoring/services")
def monitoring_services():
    with Session() as db:
        return services(db)


@app.get("/api/v1/monitoring/overview")
def overview(session_id: str | None = None):
    with Session() as db:
        five = recent(db, 5, session_id=session_id)
        one = [r for r in five if (now() - r.started_at.replace(tzinfo=timezone.utc)).total_seconds() <= 60]
        return {"services": services(db), "last_1m": summarize(one), "last_5m": summarize(five),
                "by_service": {s: summarize([r for r in five if r.service_name == s]) for s in SERVICES},
                "by_operation": {op: summarize([r for r in five if r.operation == op])
                                 for op in sorted({r.operation for r in five})},
                "generated_at": now().isoformat()}


@app.get("/api/v1/monitoring/series")
def monitoring_series(minutes: int = Query(60, ge=1, le=1440), session_id: str | None = None):
    """APM 화면용 고정 60개 시간 버킷. 원문·세션 내용은 포함하지 않는다."""
    end = now()
    bucket_seconds = max(5, minutes)  # 최대 60개 점: 5분은 5초, 24시간은 24분 단위
    start = end - timedelta(minutes=minutes)
    buckets = [{"at": (start + timedelta(seconds=i * bucket_seconds)).isoformat(), "requests": 0,
                "errors": 0, "durations": []} for i in range(60)]
    with Session() as db:
        rows = recent(db, minutes, session_id=session_id)
    for row in rows:
        index = min(59, max(0, int((utc(row.started_at) - start).total_seconds() // bucket_seconds)))
        bucket = buckets[index]
        bucket["requests"] += 1
        bucket["errors"] += row.status != "success"
        bucket["durations"].append(row.duration_ms)
    for bucket in buckets:
        durations = bucket.pop("durations")
        bucket["error_rate"] = round(bucket["errors"] / bucket["requests"] * 100, 1) if bucket["requests"] else 0
        bucket["p95_ms"] = percentile(durations, 95) if durations else None
    return {"minutes": minutes, "bucket_seconds": bucket_seconds, "summary": summarize(rows),
            "buckets": buckets, "generated_at": end.isoformat()}


def trace_dict(r):
    return {c.name: (v.isoformat() if hasattr(v, "isoformat") else v)
            for c in ApiCallTrace.__table__.columns for v in [getattr(r, c.name)]}


@app.get("/api/v1/monitoring/requests")
def requests_(minutes: int = Query(60, ge=1, le=1440), service: str | None = None, status: str | None = None,
              session_id: str | None = None, trace_id: str | None = None, limit: int = 200):
    with Session() as db:
        rows = recent(db, min(minutes, 1440), service_name=service, status=status, session_id=session_id,
                      trace_id=trace_id)
        return [trace_dict(r) for r in rows[:min(limit, 1000)]]


@app.get("/api/v1/monitoring/errors")
def errors(minutes: int = Query(60, ge=1, le=1440)):
    with Session() as db:
        return [trace_dict(r) for r in recent(db, min(minutes, 1440)) if r.status != "success"][:200]


@app.get("/api/v1/monitoring/traces/{trace_id}")
def trace(trace_id: str):
    with Session() as db:
        rows = db.query(ApiCallTrace).filter_by(trace_id=trace_id).order_by(ApiCallTrace.started_at).all()
        if not rows:
            raise AppError("TRACE_NOT_FOUND", "trace를 찾을 수 없습니다.", 404)
        return [trace_dict(r) for r in rows]


app.include_router(crm.router)
app.mount("/", StaticFiles(directory=config.ROOT / "aicc" / "static", html=True), name="ui")
