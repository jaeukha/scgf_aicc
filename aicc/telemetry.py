"""trace/span 기록과 집계(PRD §4). 본문·전사·키는 기록하지 않는다."""
import contextvars
import logging
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx

from . import config
from .db import ApiCallTrace, Session

LOG = logging.getLogger("aicc")
trace_var = contextvars.ContextVar("trace_id", default=None)
span_var = contextvars.ContextVar("span_id", default=None)
session_var = contextvars.ContextVar("session_id", default=None)  # 하위 HTTP span에 세션 전파
SERVICES = ["backend", "asr", "decider", "knowledge", "coach", "db", "crm"]


class AppError(Exception):
    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


class Cancelled(Exception):
    pass


def classify(exc):
    """예외 → (status, error_code, 안전한 메시지)."""
    if isinstance(exc, AppError):
        return ("timeout" if exc.code == "UPSTREAM_TIMEOUT" else "error"), exc.code, exc.message
    if isinstance(exc, Cancelled):
        return "cancelled", "REQUEST_CANCELLED", "취소됨"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout", "UPSTREAM_TIMEOUT", "응답 시간 초과"
    if isinstance(exc, (httpx.ConnectError, ConnectionError)):
        return "error", "UPSTREAM_UNAVAILABLE", "서비스 연결 실패"
    if isinstance(exc, httpx.HTTPStatusError):
        code = {429: "RATE_LIMITED", 503: "UPSTREAM_UNAVAILABLE", 415: "AUDIO_FORMAT_UNSUPPORTED"}.get(
            exc.response.status_code, "UPSTREAM_BAD_RESPONSE")
        return "error", code, f"HTTP {exc.response.status_code}"
    if isinstance(exc, (ValueError, KeyError, json.JSONDecodeError)):
        return "error", "SCHEMA_VALIDATION_FAILED", "응답 형식 검증 실패"
    return "error", "INTERNAL_ERROR", type(exc).__name__


def new_trace():
    trace_var.set(str(uuid.uuid4()))
    span_var.set(None)
    return trace_var.get()


def record(**row):
    # 모니터링 저장 실패는 상담 기능으로 전파하지 않는다(PRD §4.7).
    try:
        with Session() as db:
            db.add(ApiCallTrace(**row))
            db.commit()
    except Exception:
        LOG.exception("trace 저장 실패")


@contextmanager
def span(service, operation, *, session_id=None, provider=None, call_kind="inproc", model_version=None):
    """공통 타이밍/오류 래퍼. yield된 dict에 http_status_code 등을 채울 수 있다."""
    trace_id = trace_var.get() or new_trace()
    span_id, parent = str(uuid.uuid4()), span_var.get()
    session_id = session_id or session_var.get()
    token = span_var.set(span_id)
    extra = {}
    started, t0 = datetime.now(timezone.utc), time.perf_counter()
    status, code, message = "success", None, None
    try:
        yield extra
    except BaseException as exc:
        status, code, message = classify(exc)
        raise
    finally:
        span_var.reset(token)
        status, code = extra.pop("status", status), extra.pop("error_code", code)  # HTTP 응답 코드로 판정한 경우
        record(span_id=span_id, trace_id=trace_id, parent_span_id=parent,
               session_id=str(session_id) if session_id else None, service_name=service, operation=operation,
               call_kind=call_kind, provider=provider, started_at=started,
               ended_at=datetime.now(timezone.utc), duration_ms=round((time.perf_counter() - t0) * 1000, 2),
               status=status, error_code=code, error_message=message, model_version=model_version,
               model_name=(model_version or "").split("@")[0] or None, **extra)
        LOG.info(json.dumps({"trace_id": trace_id, "span_id": span_id, "service": service, "operation": operation,
                             "status": status, "error_code": code}, ensure_ascii=False))


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))], 1)


def service_status(rows):
    """rows: 최근 순 정렬된 해당 서비스 span 목록."""
    if not rows:
        return "unknown"
    n = config.UNAVAILABLE_CONSECUTIVE_FAILURES
    if len(rows) >= n and all(r.status in ("error", "timeout") for r in rows[:n]):
        return "unavailable"
    failed = sum(r.status in ("error", "timeout") for r in rows)
    return "degraded" if failed / len(rows) > config.DEGRADED_ERROR_RATE else "healthy"


def summarize(rows):
    durations = [r.duration_ms for r in rows]
    return {"requests": len(rows), "success": sum(r.status == "success" for r in rows),
            "errors": sum(r.status == "error" for r in rows), "timeouts": sum(r.status == "timeout" for r in rows),
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
            # 표본 5개 미만은 "데이터 부족"으로 표시하도록 None
            "p50_ms": percentile(durations, 50) if len(durations) >= 5 else None,
            "p95_ms": percentile(durations, 95) if len(durations) >= 5 else None}


def recent(db, minutes, **filters):
    q = db.query(ApiCallTrace).filter(
        ApiCallTrace.started_at >= datetime.now(timezone.utc) - timedelta(minutes=minutes))
    for key, value in filters.items():
        if value:
            q = q.filter(getattr(ApiCallTrace, key) == value)
    return q.order_by(ApiCallTrace.started_at.desc()).all()
