"""PostgreSQL 기준 스키마(PRD §8). SQLite는 단독 실험·테스트용."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, Uuid, create_engine, event)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config

JSONType = JSON().with_variant(JSONB(), "postgresql")


def now():
    return datetime.now(timezone.utc)


def utc(dt):
    """aware→UTC, naive는 UTC로 간주. SQLite는 오프셋을 버리므로 저장 전·전송 전에 항상 UTC로 맞춘다."""
    return None if dt is None else (dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def fk(target, nullable=True):
    return mapped_column(Uuid, ForeignKey(target), nullable=nullable)


class Base(DeclarativeBase):
    pass


class Stamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Customer(Stamped, Base):
    __tablename__ = "customers"
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(100))
    customer_type: Mapped[str | None] = mapped_column(String(50))
    external_ref: Mapped[str | None] = mapped_column(String(100))
    memo: Mapped[str | None] = mapped_column(Text)  # 고객 메모(상담별 메모와 구분)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Employee(Stamped, Base):
    __tablename__ = "employees"
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(100))
    department_name: Mapped[str] = mapped_column(String(100))
    team_name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ConsultationSession(Stamped, Base):
    __tablename__ = "consultation_sessions"
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = fk("customers.customer_id")
    assigned_employee_id: Mapped[uuid.UUID | None] = fk("employees.employee_id")
    status: Mapped[str] = mapped_column(String(20), default="active")  # created|active|paused|completed|failed
    input_mode: Mapped[str] = mapped_column(String(20), default="simulation")  # file|microphone|simulation
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_topic: Mapped[str | None] = mapped_column(String(100))
    current_stage: Mapped[str | None] = mapped_column(String(200))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    final_summary: Mapped[str | None] = mapped_column(Text)
    final_topic: Mapped[str | None] = mapped_column(String(100))
    review_status: Mapped[str] = mapped_column(String(20), default="unreviewed")  # unreviewed|reviewed|corrected
    __table_args__ = (Index("ix_sessions_started_status", "started_at", "status"),
                      Index("ix_sessions_customer", "customer_id"),
                      Index("ix_sessions_employee", "assigned_employee_id"))


class TranscriptEvent(Base):
    __tablename__ = "transcript_events"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = fk("consultation_sessions.session_id", nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20), default="unknown")  # customer|agent|unknown
    text: Mapped[str] = mapped_column(Text)
    transcript_type: Mapped[str] = mapped_column(String(10), default="final")
    source: Mapped[str] = mapped_column(String(20))  # asr|simulation|manual
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    trace_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("session_id", "sequence_no"),)


class AIAnalysisResult(Base):
    __tablename__ = "ai_analysis_results"
    analysis_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = fk("consultation_sessions.session_id", nullable=False)
    source_event_id: Mapped[uuid.UUID | None] = fk("transcript_events.event_id")
    analysis_type: Mapped[str] = mapped_column(String(30))  # routing|sentiment_text|knowledge
    topic: Mapped[str | None] = mapped_column(String(100))
    stage: Mapped[str | None] = mapped_column(String(200))
    next_step: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)  # 모델이 실제 제공한 값만
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    result_json: Mapped[dict] = mapped_column(JSONType)
    model_name: Mapped[str | None] = mapped_column(String(200))
    model_version: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_analysis_session", "session_id", "created_at"),)


class CoachSuggestion(Base):
    __tablename__ = "coach_suggestions"
    suggestion_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = fk("consultation_sessions.session_id", nullable=False)
    source_event_id: Mapped[uuid.UUID | None] = fk("transcript_events.event_id")
    trace_id: Mapped[str | None] = mapped_column(String(36))
    staff_note: Mapped[str | None] = mapped_column(Text)
    customer_question: Mapped[str | None] = mapped_column(Text)
    action_items: Mapped[list | None] = mapped_column(JSONType)
    required_checks: Mapped[list | None] = mapped_column(JSONType)
    cautions: Mapped[list | None] = mapped_column(JSONType)
    knowledge_references: Mapped[list | None] = mapped_column(JSONType)
    generation_status: Mapped[str] = mapped_column(String(20))  # success|failed|stale|off
    error_message: Mapped[str | None] = mapped_column(String(300))
    model_name: Mapped[str | None] = mapped_column(String(200))
    model_version: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConsultationNote(Stamped, Base):
    __tablename__ = "consultation_notes"
    note_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = fk("consultation_sessions.session_id", nullable=False)
    author_employee_id: Mapped[uuid.UUID] = fk("employees.employee_id", nullable=False)
    note_text: Mapped[str] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class FollowUpTask(Stamped, Base):
    __tablename__ = "follow_up_tasks"
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = fk("consultation_sessions.session_id", nullable=False)
    customer_id: Mapped[uuid.UUID | None] = fk("customers.customer_id")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    assigned_employee_id: Mapped[uuid.UUID | None] = fk("employees.employee_id")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="open")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by_employee_id: Mapped[uuid.UUID | None] = fk("employees.employee_id")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_tasks_status_due", "status", "due_at", "assigned_employee_id"),)


class ApiCallTrace(Base):
    __tablename__ = "api_call_traces"
    span_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(36))
    parent_span_id: Mapped[str | None] = mapped_column(String(36))
    session_id: Mapped[str | None] = mapped_column(String(36))
    service_name: Mapped[str] = mapped_column(String(30))
    operation: Mapped[str] = mapped_column(String(100))
    call_kind: Mapped[str] = mapped_column(String(10))  # http | inproc (논리 단계와 물리 HTTP 구분)
    provider: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(10))  # success|error|timeout|cancelled
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(300))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    request_size_bytes: Mapped[int | None] = mapped_column(Integer)
    response_size_bytes: Mapped[int | None] = mapped_column(Integer)
    queue_wait_ms: Mapped[float | None] = mapped_column(Float)
    model_name: Mapped[str | None] = mapped_column(String(200))
    model_version: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (Index("ix_traces_trace", "trace_id", "started_at"),
                      Index("ix_traces_session", "session_id", "started_at"),
                      Index("ix_traces_started", "started_at"))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    audit_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))
    actor_employee_id: Mapped[uuid.UUID | None] = fk("employees.employee_id")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    trace_id: Mapped[str | None] = mapped_column(String(36))
    before_value: Mapped[dict | None] = mapped_column(JSONType)
    after_value: Mapped[dict | None] = mapped_column(JSONType)
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class CrmSyncLink(Base):
    """AICC → Twenty 단방향 동기화 매핑. 중복 생성 방지와 재시도 상태를 보관한다."""
    __tablename__ = "crm_sync_links"
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_type: Mapped[str] = mapped_column(String(20))  # customer|session|task
    aicc_id: Mapped[str] = mapped_column(String(36))
    twenty_object: Mapped[str] = mapped_column(String(50))
    twenty_id: Mapped[str | None] = mapped_column(String(36))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (UniqueConstraint("entity_type", "aicc_id"),)


engine = create_engine(config.DATABASE_URL, pool_pre_ping=True,
                       connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {})
if engine.dialect.name == "sqlite":
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
Session = sessionmaker(engine, expire_on_commit=False)


SEED_EMPLOYEES = [("김상담", "보증상담부", "1팀"), ("이지원", "보증상담부", "2팀"), ("박관리", "고객지원부", None)]
SEED_CUSTOMERS = [("시연 고객 001", "소상공인"), ("시연 고객 002", "예비창업자"), ("시연 고객 003", "소상공인")]


def seed(db):
    """가상 직원·고객 시드. 이미 있으면 건너뛴다."""
    if db.query(Employee).first():
        return
    db.add_all(Employee(display_name=n, department_name=d, team_name=t) for n, d, t in SEED_EMPLOYEES)
    db.add_all(Customer(display_name=n, customer_type=t, external_ref=f"DEMO-{i:03d}")
               for i, (n, t) in enumerate(SEED_CUSTOMERS, 1))
    db.commit()
