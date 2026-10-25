"""CRM API(PRD §3.7, §7.2). AI 원본과 직원 확정값을 분리하고 변경은 audit_events에 남긴다."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_

from . import twenty_sync
from .db import (AIAnalysisResult, AuditEvent, CoachSuggestion, ConsultationNote, ConsultationSession, Customer,
                 Employee, FollowUpTask, Session, TranscriptEvent, now, utc)
from .telemetry import AppError, trace_var

router = APIRouter(prefix="/api/v1/crm")
TaskStatus = Literal["open", "in_progress", "blocked", "completed", "cancelled"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)  # 허용 필드만, 앞뒤 공백 제거 후 길이 검증


def plain(obj):
    return {c.name: plain_value(getattr(obj, c.name)) for c in obj.__table__.columns}


def need(db, model, key, what):
    if key is not None and db.get(model, key) is None:
        raise AppError("NOT_FOUND", f"{what}을(를) 찾을 수 없습니다.", 404)
    return db.get(model, key) if key is not None else None


def audit(db, entity_type, entity_id, action, actor, before, after):
    db.add(AuditEvent(entity_type=entity_type, entity_id=str(entity_id), action=action, actor_employee_id=actor,
                      trace_id=trace_var.get(), before_value=before, after_value=after))


def apply(obj, changes):
    """변경된 허용 필드만 before/after로 반환."""
    before, after = {}, {}
    for k, v in changes.items():
        old = getattr(obj, k)
        if old != v:
            before[k], after[k] = plain_value(old), plain_value(v)
            setattr(obj, k, v)
    return before, after


def plain_value(v):
    return str(v) if isinstance(v, uuid.UUID) else v.isoformat() if isinstance(v, datetime) else v


def overdue(t):
    due = t.due_at and (t.due_at if t.due_at.tzinfo else t.due_at.replace(tzinfo=timezone.utc))
    return bool(due and t.status not in ("completed", "cancelled") and due < now())


def task_dict(t):
    return plain(t) | {"overdue": overdue(t)}


def page(q, page_no, size):
    total = q.count()
    return {"total": total, "page": page_no, "size": size, "items": q.offset((page_no - 1) * size).limit(size).all()}


@router.get("/dashboard")
def dashboard():
    with Session() as db:
        tasks = db.query(FollowUpTask).all()
        sessions = db.query(ConsultationSession)
        return {"sessions_total": sessions.count(),
                "sessions_active": sessions.filter(ConsultationSession.status.in_(["active", "paused"])).count(),
                "sessions_unreviewed": sessions.filter_by(review_status="unreviewed", status="completed").count(),
                "tasks_open": sum(t.status in ("open", "in_progress", "blocked") for t in tasks),
                "tasks_overdue": sum(overdue(t) for t in tasks)}


# ---------- 고객 / 직원 ----------
class CustomerIn(Strict):
    display_name: str = Field(min_length=1, max_length=100)
    customer_type: str | None = Field(None, max_length=50)
    external_ref: str | None = Field(None, max_length=100)
    memo: str | None = Field(None, max_length=2000)


class CustomerPatch(Strict):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    customer_type: str | None = Field(None, max_length=50)
    memo: str | None = Field(None, max_length=2000)
    is_active: bool | None = None


@router.get("/customers")
def customers(q: str | None = None, page_no: int = Query(1, ge=1, le=10**6), size: int = Query(20, ge=1, le=100)):
    with Session() as db:
        query = db.query(Customer)
        if q:
            query = query.filter(Customer.display_name.contains(q, autoescape=True))
        p = page(query.order_by(Customer.display_name), page_no, min(size, 100))
        return p | {"items": [plain(c) for c in p["items"]]}


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn, x_employee_id: uuid.UUID | None = Header(None)):
    with Session() as db:
        c = Customer(**body.model_dump())
        db.add(c)
        db.flush()
        audit(db, "customer", c.customer_id, "create", x_employee_id, None, body.model_dump())
        db.commit()
        return plain(c)


@router.get("/customers/{cid}")
def customer(cid: uuid.UUID):
    with Session() as db:
        c = need(db, Customer, cid, "고객")
        sessions = db.query(ConsultationSession).filter_by(customer_id=cid).order_by(
            ConsultationSession.started_at.desc()).all()
        return plain(c) | {"timeline": [plain(s) for s in sessions]}


@router.patch("/customers/{cid}")
def patch_customer(cid: uuid.UUID, body: CustomerPatch, x_employee_id: uuid.UUID | None = Header(None)):
    with Session() as db:
        c = need(db, Customer, cid, "고객")
        before, after = apply(c, body.model_dump(exclude_unset=True))
        if after:
            audit(db, "customer", cid, "update", x_employee_id, before, after)
        db.commit()
        return plain(c)


@router.get("/employees")
def employees():
    with Session() as db:
        return [plain(e) for e in db.query(Employee).filter_by(is_active=True).order_by(Employee.display_name)]


# ---------- 상담 ----------
@router.get("/sessions")
def sessions(status: str | None = None, topic: str | None = None, employee_id: uuid.UUID | None = None,
             customer_id: uuid.UUID | None = None, task_status: str | None = None,
             date_from: datetime | None = None, date_to: datetime | None = None,
             sort: Literal["started_at", "-started_at"] = "-started_at", page_no: int = Query(1, ge=1, le=10**6),
             size: int = Query(20, ge=1, le=100)):
    with Session() as db:
        q = db.query(ConsultationSession)
        if status:
            q = q.filter(ConsultationSession.status == status)
        if topic:
            q = q.filter(or_(ConsultationSession.current_topic == topic, ConsultationSession.final_topic == topic))
        if employee_id:
            q = q.filter(ConsultationSession.assigned_employee_id == employee_id)
        if customer_id:
            q = q.filter(ConsultationSession.customer_id == customer_id)
        if date_from:  # SQLite는 오프셋을 버리고 비교하므로 항상 UTC로 맞춘다
            q = q.filter(ConsultationSession.started_at >= utc(date_from))
        if date_to:
            q = q.filter(ConsultationSession.started_at <= utc(date_to))
        if task_status:
            q = q.filter(ConsultationSession.session_id.in_(
                db.query(FollowUpTask.session_id).filter(FollowUpTask.status == task_status)))
        col = ConsultationSession.started_at
        p = page(q.order_by(col if sort == "started_at" else col.desc()), page_no, min(size, 100))
        names = {e.employee_id: e.display_name for e in db.query(Employee)}
        cust = {c.customer_id: c.display_name for c in db.query(Customer)}
        items = []
        for s in p["items"]:
            tasks = db.query(FollowUpTask).filter_by(session_id=s.session_id).all()
            items.append(plain(s) | {"employee_name": names.get(s.assigned_employee_id),
                                     "customer_name": cust.get(s.customer_id),
                                     "tasks_open": sum(t.status in ("open", "in_progress", "blocked") for t in tasks)})
        return p | {"items": items}


@router.get("/sessions/{sid}")
def session_detail(sid: uuid.UUID):
    from .main import session_events  # 순환 import 회피
    with Session() as db:
        s = need(db, ConsultationSession, sid, "상담 세션")
        return plain(s) | {
            "transcripts": [plain(t) for t in db.query(TranscriptEvent).filter_by(session_id=sid).order_by(
                TranscriptEvent.sequence_no)],
            "analyses": [plain(a) for a in db.query(AIAnalysisResult).filter_by(session_id=sid).order_by(
                AIAnalysisResult.created_at)],
            "suggestions": [plain(c) for c in db.query(CoachSuggestion).filter_by(session_id=sid).order_by(
                CoachSuggestion.created_at)],
            "notes": [plain(n) for n in db.query(ConsultationNote).filter_by(session_id=sid, is_deleted=False)],
            "tasks": [task_dict(t) for t in db.query(FollowUpTask).filter_by(session_id=sid)],
            "audit": [plain(a) for a in db.query(AuditEvent).filter_by(entity_type="session", entity_id=str(sid))
                      .order_by(AuditEvent.occurred_at)],
            "events": session_events(db, sid), "twenty": twenty_sync.status(db, sid)}


@router.post("/sessions/{sid}/sync-twenty")
def sync_twenty(sid: uuid.UUID):
    """Twenty PoC로 즉시 push(재시도 겸용, 멱등)."""
    return twenty_sync.sync_session(sid)


class Assignment(Strict):
    assigned_employee_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None


@router.patch("/sessions/{sid}/assignment")
def assign(sid: uuid.UUID, body: Assignment, x_employee_id: uuid.UUID | None = Header(None)):
    with Session() as db:
        s = need(db, ConsultationSession, sid, "상담 세션")
        changes = body.model_dump(exclude_unset=True)
        need(db, Employee, changes.get("assigned_employee_id"), "직원")
        need(db, Customer, changes.get("customer_id"), "고객")
        before, after = apply(s, changes)
        if after:
            audit(db, "session", sid, "assign", x_employee_id, before, after)
        db.commit()
        return plain(s)


class NoteIn(Strict):
    note_text: str = Field(min_length=1, max_length=4000)
    author_employee_id: uuid.UUID


@router.post("/sessions/{sid}/notes", status_code=201)
def add_note(sid: uuid.UUID, body: NoteIn):
    with Session() as db:
        need(db, ConsultationSession, sid, "상담 세션")
        need(db, Employee, body.author_employee_id, "직원")
        n = ConsultationNote(session_id=sid, **body.model_dump())
        db.add(n)
        db.flush()
        audit(db, "session", sid, "note_add", body.author_employee_id, None, {"note_id": str(n.note_id)})
        db.commit()
        return plain(n)


class Review(Strict):
    review_status: Literal["reviewed", "corrected"]
    final_topic: str | None = Field(None, max_length=100)
    final_summary: str | None = Field(None, max_length=8000)


@router.patch("/sessions/{sid}/review")
def review(sid: uuid.UUID, body: Review, x_employee_id: uuid.UUID | None = Header(None)):
    """직원 확정값은 final_* 필드에만 저장. AI 원본(ai_summary, current_topic, 분석 이력)은 덮어쓰지 않는다."""
    with Session() as db:
        s = need(db, ConsultationSession, sid, "상담 세션")
        before, after = apply(s, body.model_dump(exclude_unset=True))
        if after:
            audit(db, "session", sid, "review", x_employee_id, before, after)
        db.commit()
        twenty_sync.schedule(sid)
        return plain(s)


# ---------- 후속 업무 ----------
class TaskIn(Strict):
    session_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    assigned_employee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    source: Literal["ai_suggested", "manual"] = "manual"


class TaskPatch(Strict):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    assigned_employee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high"] | None = None
    status: TaskStatus | None = None


@router.get("/tasks")
def tasks(status: TaskStatus | None = None, employee_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None,
          overdue_only: bool = False):
    with Session() as db:
        q = db.query(FollowUpTask)
        if status:
            q = q.filter_by(status=status)
        if employee_id:
            q = q.filter_by(assigned_employee_id=employee_id)
        if session_id:
            q = q.filter_by(session_id=session_id)
        rows = [task_dict(t) for t in q.order_by(FollowUpTask.due_at.is_(None), FollowUpTask.due_at)]
        return [t for t in rows if t["overdue"]] if overdue_only else rows


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, x_employee_id: uuid.UUID | None = Header(None)):
    """AI action_items는 상담원이 선택해 이 API를 호출할 때만 업무가 된다."""
    with Session() as db:
        s = need(db, ConsultationSession, body.session_id, "상담 세션")
        need(db, Employee, body.assigned_employee_id, "직원")
        t = FollowUpTask(**body.model_dump() | {"due_at": utc(body.due_at)}, customer_id=s.customer_id,
                         created_by_employee_id=x_employee_id)
        db.add(t)
        db.flush()
        audit(db, "task", t.task_id, "create", x_employee_id, None,
              {k: plain_value(v) for k, v in body.model_dump().items()})
        db.commit()
        twenty_sync.schedule(t.session_id)
        return task_dict(t)


@router.patch("/tasks/{tid}")
def patch_task(tid: uuid.UUID, body: TaskPatch, x_employee_id: uuid.UUID | None = Header(None)):
    with Session() as db:
        t = need(db, FollowUpTask, tid, "후속 업무")
        changes = body.model_dump(exclude_unset=True)
        if "due_at" in changes:
            changes["due_at"] = utc(changes["due_at"])
        need(db, Employee, changes.get("assigned_employee_id"), "직원")
        before, after = apply(t, changes)
        if "status" in after:
            t.completed_at = now() if t.status == "completed" else None
        if after:
            audit(db, "task", tid, "update", x_employee_id, before, after)
        db.commit()
        twenty_sync.schedule(t.session_id)
        return task_dict(t)


@router.get("/audit-events")
def audit_events(entity_type: str | None = None, entity_id: str | None = None, limit: int = Query(200, ge=1, le=1000)):
    # ponytail: 인증 미구현 시연. 역할 기반 권한이 생기면 여기서 관리자만 허용
    with Session() as db:
        q = db.query(AuditEvent)
        if entity_type:
            q = q.filter_by(entity_type=entity_type)
        if entity_id:
            q = q.filter_by(entity_id=entity_id)
        return [plain(a) for a in q.order_by(AuditEvent.occurred_at.desc()).limit(min(limit, 1000))]
