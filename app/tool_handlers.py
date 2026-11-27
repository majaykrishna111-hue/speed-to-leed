"""Server-side implementations of the six tools in prompts/tools.json.

The model only ever sees what these return. Anything that must never go wrong (phone number,
service-area gate, which slots are bookable, double bookings) is enforced here, not in the prompt."""
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .calendar_slots import HORIZON_DAYS, DAY_KEYS, compute_slots
from .models import Client, EventLog, Lead, SlotOffer, as_utc

log = logging.getLogger(__name__)


# ---------- Input schemas (mirror tools.json; reject anything else) ----------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveLeadDetails(_Strict):
    service_type: Optional[Literal["repair", "maintenance", "replacement_quote", "other"]] = None
    issue_summary: Optional[str] = Field(None, max_length=300)
    zip_code: Optional[str] = Field(None, pattern=r"^[0-9]{5}$")
    urgency: Optional[Literal["emergency_today", "within_48_hours", "this_week",
                              "flexible", "just_researching"]] = None
    full_name: Optional[str] = Field(None, max_length=100)
    email: Optional[EmailStr] = None


class CheckServiceArea(_Strict):
    zip_code: str = Field(pattern=r"^[0-9]{5}$")


class GetAvailableSlots(_Strict):
    urgency: Literal["emergency_today", "within_48_hours", "this_week", "flexible"]
    earliest_date: Optional[date] = None
    time_of_day: Literal["morning", "afternoon", "any"] = "any"


class BookAppointment(_Strict):
    slot_id: str
    full_name: str = Field(min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    notes: str = Field(max_length=500)


class EscalateToHuman(_Strict):
    reason: Literal["requested_human", "complex_question", "complaint", "existing_appointment",
                    "pricing_negotiation", "emergency", "other"]
    urgency: Literal["normal", "urgent"]
    summary: str = Field(max_length=600)


class CloseConversation(_Strict):
    reason: Literal["out_of_area", "not_interested", "already_handled", "wrong_number",
                    "opt_out_request"]


SCHEMAS = {
    "save_lead_details": SaveLeadDetails, "check_service_area": CheckServiceArea,
    "get_available_slots": GetAvailableSlots, "book_appointment": BookAppointment,
    "escalate_to_human": EscalateToHuman, "close_conversation": CloseConversation,
}


@dataclass
class ToolContext:
    db: Session
    client: Client
    lead: Lead
    deps: Any
    escalated: bool = False
    opt_out: bool = False
    booked_label: Optional[str] = None
    calls: list[str] = field(default_factory=list)


def log_event(db: Session, type_: str, lead: Optional[Lead] = None,
              client: Optional[Client] = None, **data) -> None:
    db.add(EventLog(type=type_, lead_id=lead.id if lead else None,
                    client_id=(client.id if client else (lead.client_id if lead else None)),
                    data=data or None))


def execute_tool(ctx: ToolContext, name: str, raw_input: dict) -> tuple[dict, bool]:
    """Run one tool call. Returns (result, is_error)."""
    ctx.calls.append(name)
    schema = SCHEMAS.get(name)
    if schema is None:
        return {"error": f"Unknown tool {name}."}, True
    try:
        args = schema.model_validate(raw_input or {})
    except ValidationError as exc:
        msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return {"error": f"Invalid input: {msg}"}, True
    try:
        result = HANDLERS[name](ctx, args)
    except Exception as exc:  # never let a tool crash the conversation
        log.exception("Tool %s failed", name)
        log_event(ctx.db, "tool_error", ctx.lead, tool=name, error=str(exc)[:300])
        return {"error": "This system is temporarily unavailable."}, True
    log_event(ctx.db, f"tool:{name}", ctx.lead, input=json.loads(args.model_dump_json()),
              result=result)
    ctx.db.flush()
    return result, False


# ---------- Handlers ----------

def save_lead_details(ctx: ToolContext, a: SaveLeadDetails) -> dict:
    lead = ctx.lead
    for key, value in a.model_dump(exclude_none=True).items():
        if key == "zip_code" and value != lead.zip_code:
            lead.in_service_area = None  # new ZIP must be re-checked
        setattr(lead, key, value)
    return {"saved": True}


def check_service_area(ctx: ToolContext, a: CheckServiceArea) -> dict:
    cfg = ctx.client.config
    zips = set(cfg.get("service_zips", []))
    prefixes = tuple(cfg.get("service_zip_prefixes", []))
    in_area = a.zip_code in zips or (bool(prefixes) and a.zip_code.startswith(prefixes))
    ctx.lead.zip_code = a.zip_code
    ctx.lead.in_service_area = in_area
    if in_area:
        return {"in_area": True, "area_name": cfg.get("zip_area_names", {}).get(a.zip_code)}
    result: dict = {"in_area": False}
    if cfg.get("out_of_area_referral"):
        result["referral"] = cfg["out_of_area_referral"]
    return result


def _busy(ctx: ToolContext, days: int) -> list:
    tz = ZoneInfo(ctx.client.timezone)
    start = ctx.deps.now().astimezone(tz)
    return ctx.deps.calendar.busy_intervals(ctx.client.calendar_id, start, start + timedelta(days=days + 1))


def get_available_slots(ctx: ToolContext, a: GetAvailableSlots) -> dict:
    if ctx.lead.in_service_area is not True:
        return {"error": "Confirm the ZIP code with check_service_area before offering times."}
    cfg = ctx.client.config
    horizon = HORIZON_DAYS.get(a.urgency, int(cfg.get("horizon_days", 14)))
    if a.earliest_date:
        horizon = max(horizon, (a.earliest_date - ctx.deps.now().date()).days + 3)
    horizon = min(horizon, 60)
    busy = _busy(ctx, horizon)
    slots = compute_slots(cfg | {"horizon_days": horizon}, ctx.client.timezone, ctx.deps.now(), busy,
                          a.urgency if not a.earliest_date else "flexible",
                          a.earliest_date, a.time_of_day)
    if not slots:
        return {"slots": [], "note": "No openings match. Escalate so a team member can schedule."}
    out = []
    for s in slots:
        offer = SlotOffer(id="s_" + secrets.token_hex(3), lead_id=ctx.lead.id,
                          start=s.start, end=s.end, label=s.label)
        ctx.db.add(offer)
        out.append({"slot_id": offer.id, "label": s.label})
    return {"slots": out}


def book_appointment(ctx: ToolContext, a: BookAppointment) -> dict:
    lead, client, cfg = ctx.lead, ctx.client, ctx.client.config
    if lead.status == "booked":
        when = as_utc(lead.appointment_start).astimezone(ZoneInfo(client.timezone))
        return {"status": "already_booked",
                "when_label": f"{when:%a %b} {when.day}",
                "note": "This lead already has an appointment. Escalate to change it."}
    if lead.in_service_area is not True:
        return {"error": "Service area not confirmed for this lead."}
    offer = ctx.db.get(SlotOffer, a.slot_id)
    if offer is None or offer.lead_id != lead.id or offer.used:
        return {"error": "Unknown slot_id. Only book a slot_id returned by get_available_slots "
                         "for this lead. Fetch slots again."}
    start, end = as_utc(offer.start), as_utc(offer.end)
    if start <= ctx.deps.now():
        return {"status": "slot_unavailable"}
    busy = ctx.deps.calendar.busy_intervals(client.calendar_id, start, end)
    taken = sum(1 for b_start, b_end in busy if b_start < end and start < b_end)
    if taken >= int(cfg.get("slot_capacity", 1)):
        return {"status": "slot_unavailable"}

    lead.full_name = a.full_name
    if a.email:
        lead.email = str(a.email)
    service = (lead.service_type or "service").replace("_", " ")
    description = "\n".join([
        f"Customer: {a.full_name}",
        f"Phone: {lead.phone}",  # always from the server, never from the model
        f"Email: {lead.email or 'not provided'}",
        f"ZIP: {lead.zip_code}",
        f"Service: {service}",
        f"Urgency: {lead.urgency or 'not stated'}",
        f"Notes: {a.notes}",
        "",
        f"Booked by the SMS scheduling assistant. Lead #{lead.id}.",
    ])
    event_id = ctx.deps.calendar.create_event(
        client.calendar_id, start, end,
        f"{cfg.get('event_title_prefix', 'Service visit')}: {a.full_name} ({service})",
        description, client.timezone)
    offer.used = True
    lead.status = "booked"
    lead.appointment_start = start
    lead.appointment_event_id = event_id
    ctx.booked_label = offer.label
    if cfg.get("notify_on_booking", True):
        ctx.deps.alerts.send(cfg, f"New booking: {a.full_name}, {offer.label}",
                             f"{description}\n\nConversation: {inbox_link(ctx)}")
    return {"status": "confirmed", "when_label": offer.label,
            "what_to_expect": cfg.get("what_to_expect", "")}


def inbox_link(ctx: ToolContext) -> str:
    s = ctx.deps.settings
    return f"{s.public_base_url.rstrip('/')}/admin/leads/{ctx.lead.id}"  # login required to open


def owner_response_time(cfg: dict, tz_name: str, now: datetime) -> str:
    """Plain-English 'when will a person respond', from office_hours {"mon": ["07:00","18:00"], ...}."""
    hours = cfg.get("office_hours", {})
    local = now.astimezone(ZoneInfo(tz_name))
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        span = hours.get(DAY_KEYS[day.weekday()])
        if not span:
            continue
        open_t = datetime.combine(day, datetime.strptime(span[0], "%H:%M").time(), local.tzinfo)
        close_t = datetime.combine(day, datetime.strptime(span[1], "%H:%M").time(), local.tzinfo)
        if offset == 0 and open_t <= local < close_t:
            return "shortly (the office is open now)"
        if local < open_t:
            h = open_t.hour % 12 or 12
            when = f"{h}{'am' if open_t.hour < 12 else 'pm'}"
            if offset == 0:
                return f"when the office opens at {when} today"
            if offset == 1:
                return f"when the office opens at {when} tomorrow"
            return f"when the office opens {open_t:%A} at {when}"
    return "as soon as possible"


def escalate_to_human(ctx: ToolContext, a: EscalateToHuman) -> dict:
    lead = ctx.lead
    ctx.escalated = True
    recent = [m for m in lead.messages if m.role in ("lead", "agent", "staff")][-6:]
    transcript = "\n".join(f"{m.role.upper()}: {m.body}" for m in recent)
    who = lead.full_name or "Unknown name"
    text = (f"Lead: {who}, {lead.phone}\nReason: {a.reason}\nSummary: {a.summary}\n\n"
            f"Recent messages:\n{transcript}\n\nAutomated replies are paused for this lead. "
            f"Reply or resume here: {inbox_link(ctx)}")
    ctx.deps.alerts.send(ctx.client.config, f"Lead needs a person: {who} ({a.reason})", text,
                         urgent=a.urgency == "urgent")
    if a.reason == "emergency":
        return {"status": "owner_notified_urgent",
                "team_will_respond": "as soon as possible; the owner got an urgent alert"}
    return {"status": "owner_notified",
            "team_will_respond": owner_response_time(ctx.client.config, ctx.client.timezone, ctx.deps.now())}


def close_conversation(ctx: ToolContext, a: CloseConversation) -> dict:
    ctx.lead.status = "closed"
    ctx.lead.closed_reason = a.reason
    if a.reason == "opt_out_request":
        ctx.opt_out = True
        return {"status": "opted_out", "instruction": "Reply with exactly NO_REPLY."}
    return {"status": "closed"}


HANDLERS = {
    "save_lead_details": save_lead_details, "check_service_area": check_service_area,
    "get_available_slots": get_available_slots, "book_appointment": book_appointment,
    "escalate_to_human": escalate_to_human, "close_conversation": close_conversation,
}


def offers_for(db: Session, lead: Lead) -> list[SlotOffer]:
    return list(db.scalars(select(SlotOffer).where(SlotOffer.lead_id == lead.id).order_by(SlotOffer.created_at)))
