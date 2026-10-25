"""Twenty CRM PoC 연계: AICC(PostgreSQL 원본) → Twenty 단방향 push, 공개 REST API만 사용.

매핑: 고객 → company, 상담 세션 → aiccConsultation(사용자 정의 객체), 후속 업무 → task(+taskTarget).
AICC가 소유한 필드는 동기화 때마다 덮어쓴다. Twenty에서의 수정은 AICC로 가져오지 않는다(양방향 동기화 금지).
중복 방지는 crm_sync_links(entity_type, aicc_id) 유일키로, 재시도는 같은 함수를 다시 호출하면 된다(멱등).
전사 원문은 보내지 않고 요약·주제·상태만 보낸다.
"""
import threading
from datetime import timedelta, timezone

import httpx

from . import config
from .db import ConsultationSession, CrmSyncLink, Customer, FollowUpTask, Session, now, utc
from .telemetry import AppError, session_var, span, trace_var

KST = timezone(timedelta(hours=9), "KST")
TASK_STATUS = {"open": "TODO", "in_progress": "IN_PROGRESS", "blocked": "IN_PROGRESS",
               "completed": "DONE", "cancelled": "DONE"}  # cancelled는 본문에 표시


def request(method, path, body=None):
    with span("crm", f"twenty.{method.lower()} {path.split('/')[0]}", provider="twenty", call_kind="http") as extra:
        r = httpx.request(method, f"{config.TWENTY_BASE_URL.rstrip('/')}/rest/{path}", json=body,
                          headers={"Authorization": f"Bearer {config.TWENTY_API_KEY}", "X-Trace-Id": trace_var.get() or ""},
                          timeout=config.TWENTY_TIMEOUT_SECONDS)
        extra["http_status_code"] = r.status_code
        if r.status_code != 404:
            r.raise_for_status()
        return r


def upsert(db, entity_type, aicc_id, obj, payload, on_create=None):
    """링크가 있으면 PATCH, 없거나 Twenty에서 삭제됐으면(404) 새로 생성."""
    link = db.query(CrmSyncLink).filter_by(entity_type=entity_type, aicc_id=str(aicc_id)).first()
    if link is None:
        link = CrmSyncLink(entity_type=entity_type, aicc_id=str(aicc_id), twenty_object=obj)
        db.add(link)
    try:
        patched = link.twenty_id and request("PATCH", f"{obj}/{link.twenty_id}", payload).status_code != 404
        if not patched:
            r = request("POST", obj, payload)
            link.twenty_id = next(iter(r.json()["data"].values()))["id"]
            if on_create:
                on_create(link.twenty_id)
        link.synced_at, link.last_error = now(), None
    except Exception as exc:
        link.last_error = getattr(exc, "message", None) or type(exc).__name__
        db.commit()
        raise
    db.commit()
    return link.twenty_id


_locks: dict[str, threading.Lock] = {}


def sync_session(sid):
    if not config.TWENTY_ENABLED:
        raise AppError("CRM_SYNC_DISABLED", "Twenty 연계가 꺼져 있습니다(TWENTY_ENABLED=false).", 409)
    session_var.set(sid)
    # 같은 세션의 동시 동기화(종료·검토·업무 변경 직후 연속 호출)가 중복 POST하지 않도록 직렬화.
    # ponytail: 프로세스 내 잠금. 백엔드를 여러 프로세스로 띄우면 DB advisory lock으로 교체
    with _locks.setdefault(str(sid), threading.Lock()), Session() as db:
        s = db.get(ConsultationSession, sid)
        if s is None:
            raise AppError("SESSION_NOT_FOUND", "상담 세션을 찾을 수 없습니다.", 404)
        company_id = None
        if s.customer_id:
            c = db.get(Customer, s.customer_id)
            company_id = upsert(db, "customer", c.customer_id, "companies", {"name": c.display_name})
        topic = s.final_topic or s.current_topic
        consultation = upsert(db, "session", sid, "aiccConsultations", {
            "name": f"{utc(s.started_at).astimezone(KST):%Y-%m-%d %H:%M} {topic or '주제 미확정'}", "aiccSessionId": str(sid),
            "topic": topic, "reviewStatus": s.review_status,
            # 직원 확정 요약 우선. AI 요약만 있으면 그 사실을 표시
            "summary": s.final_summary or (f"[AI 요약·{s.review_status}] {s.ai_summary}" if s.ai_summary else None),
            "startedAt": utc(s.started_at).isoformat(), "companyId": company_id})
        tasks = []
        for t in db.query(FollowUpTask).filter_by(session_id=sid):
            body = (f"{t.description or ''}\n\n- AICC 상태: {t.status} · 우선순위: {t.priority} · "
                    f"출처: {'AI 제안(상담원 확인)' if t.source == 'ai_suggested' else '수동'} · AICC 세션 {sid}")
            link_target = (lambda tid: request("POST", "taskTargets", {"taskId": tid, "targetCompanyId": company_id})
                           ) if company_id else None
            tasks.append(upsert(db, "task", t.task_id, "tasks", {
                "title": t.title, "status": TASK_STATUS[t.status], "bodyV2": {"markdown": body},
                "dueAt": utc(t.due_at).isoformat() if t.due_at else None}, link_target))
        return {"company_id": company_id, "consultation_id": consultation, "task_ids": tasks}


def healthy():
    try:
        return httpx.get(config.TWENTY_BASE_URL.rstrip("/") + "/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def schedule(sid):
    """종료·검토·업무 변경 후 백그라운드 push. 실패는 crm_sync_links.last_error와 trace에 남고 상담 기능을 막지 않는다."""
    if config.TWENTY_ENABLED:
        # ponytail: 스레드 즉시 실행·자동 재시도 없음. 실패분은 동기화 버튼(재호출)로 재시도
        threading.Thread(target=lambda: _quiet(sid), daemon=True).start()


def _quiet(sid):
    try:
        sync_session(sid)
    except Exception:
        pass


def status(db, sid):
    links = db.query(CrmSyncLink).filter(CrmSyncLink.aicc_id.in_(
        [str(sid), str(db.get(ConsultationSession, sid).customer_id)]
        + [str(t.task_id) for t in db.query(FollowUpTask).filter_by(session_id=sid)])).all()
    return {"enabled": config.TWENTY_ENABLED, "links": [
        {"entity_type": l.entity_type, "twenty_object": l.twenty_object, "twenty_id": l.twenty_id,
         "synced_at": l.synced_at.isoformat() if l.synced_at else None, "last_error": l.last_error} for l in links]}
