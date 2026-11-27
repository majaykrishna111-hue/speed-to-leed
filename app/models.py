"""Database tables. Works on Postgres in production and SQLite for local runs and tests."""
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        TypeDecorator, UniqueConstraint, create_engine)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores every datetime as UTC and always returns timezone-aware UTC.
    SQLite has no timezone support and would otherwise save 10am Eastern as plain '10:00'."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime: pass a timezone-aware value")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: UTCDateTime()}


class Client(Base):
    """One row per business you sell to. Business-specific settings live in `config` (JSON)."""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    twilio_number: Mapped[str] = mapped_column(String(20), unique=True)  # E.164
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    calendar_id: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    leads: Mapped[list["Lead"]] = relationship(back_populates="client")


class Lead(Base):
    """One row per (business, phone number). Holds qualification data and conversation state."""
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("client_id", "phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20))  # E.164
    source: Mapped[str] = mapped_column(String(20))  # web_form | missed_call | inbound_sms

    # active | booked | escalated | closed
    status: Mapped[str] = mapped_column(String(20), default="active")
    closed_reason: Mapped[Optional[str]] = mapped_column(String(40))
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)  # True = a human owns it
    paused_reason: Mapped[Optional[str]] = mapped_column(String(60))

    # Qualification data (filled by save_lead_details / check_service_area)
    full_name: Mapped[Optional[str]] = mapped_column(String(100))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    service_type: Mapped[Optional[str]] = mapped_column(String(40))
    issue_summary: Mapped[Optional[str]] = mapped_column(Text)
    zip_code: Mapped[Optional[str]] = mapped_column(String(5))
    in_service_area: Mapped[Optional[bool]] = mapped_column(Boolean)
    urgency: Mapped[Optional[str]] = mapped_column(String(30))
    form_payload: Mapped[Optional[dict[str, Any]]] = mapped_column()

    # Booking
    appointment_start: Mapped[Optional[datetime]] = mapped_column()
    appointment_event_id: Mapped[Optional[str]] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_message_at: Mapped[Optional[datetime]] = mapped_column()

    client: Mapped[Client] = relationship(back_populates="leads")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="lead", order_by="Message.id")


class Message(Base):
    """Conversation memory. role: event | lead | agent | staff | system_notice."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    twilio_sid: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class SlotOffer(Base):
    """Every time slot the agent offered. book_appointment only accepts IDs from this table."""
    __tablename__ = "slot_offers"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    start: Mapped[datetime] = mapped_column()
    end: Mapped[datetime] = mapped_column()
    label: Mapped[str] = mapped_column(String(100))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class StaffUser(Base):
    """A login for the staff inbox. client_id=None means operator (you): sees every business."""
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column()


class EventLog(Base):
    """Audit trail: tool calls, escalations, errors, opt-outs. Useful for debugging and client reports."""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[Optional[int]] = mapped_column(ForeignKey("leads.id"), index=True)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[Optional[dict[str, Any]]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def normalize_database_url(url: str) -> str:
    """Hosts like Render and Heroku hand out postgres:// or postgresql:// URLs, which SQLAlchemy maps
    to the psycopg2 driver. This app ships psycopg 3, so point those URLs at it."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_session_factory(database_url: str):
    database_url = normalize_database_url(database_url)
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite drops tz info; treat naive datetimes from the DB as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
