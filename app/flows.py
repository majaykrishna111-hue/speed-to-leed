"""What happens when each trigger arrives. Each handler records the event and returns the lead id
if the agent should reply, or None if it must stay quiet (opted out, human owns it, no consent)."""
import re
from datetime import timedelta
from typing import Optional

import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapters import Deps
from .agent import send_sms
from .models import Client, Lead, Message, as_utc
from .tool_handlers import ToolContext, inbox_link, log_event

# Mirrors Twilio's default Advanced Opt-Out keywords so our records match what Twilio enforces.
STOP_WORDS = {"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
              "REVOKE", "OPTOUT", "OPT OUT"}
START_WORDS = {"START", "UNSTOP", "YES"}
HELP_WORDS = {"HELP", "INFO"}
RECENT = timedelta(hours=24)


def normalize_phone(raw: str, region: str = "US") -> Optional[str]:
    try:
        num = phonenumbers.parse(raw or "", region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def _keyword(body: str) -> str:
    return re.sub(r"[^A-Z ]", "", (body or "").upper()).strip()


def get_or_create_lead(db: Session, client: Client, phone: str, source: str) -> Lead:
    lead = db.scalar(select(Lead).where(Lead.client_id == client.id, Lead.phone == phone))
    if lead is None:
        lead = Lead(client_id=client.id, phone=phone, source=source)
        db.add(lead)
        db.flush()
    return lead


def _alert(db: Session, deps: Deps, client: Client, lead: Lead, title: str, text: str, urgent=False):
    ctx = ToolContext(db=db, client=client, lead=lead, deps=deps)
    deps.alerts.send(client.config, title, f"{text}\nLead: {lead.full_name or ''} {lead.phone}\n{inbox_link(ctx)}",
                     urgent=urgent)


def _is_recent(lead: Lead, deps: Deps) -> bool:
    return bool(lead.last_message_at and deps.now() - as_utc(lead.last_message_at) < RECENT)


def _start_new_conversation(db: Session, lead: Lead, source: str, event_text: str) -> None:
    lead.source = source
    if lead.status in ("closed", "booked") and lead.closed_reason != "opt_out_request":
        lead.status, lead.closed_reason = "active", None
    db.add(Message(lead_id=lead.id, role="event", body=f"[NEW_LEAD] {event_text}"))


def handle_inbound_sms(db: Session, deps: Deps, client: Client, from_number: str, body: str,
                       sid: Optional[str] = None) -> Optional[int]:
    phone = normalize_phone(from_number) or from_number
    lead = get_or_create_lead(db, client, phone, "inbound_sms")
    db.add(Message(lead_id=lead.id, role="lead", body=(body or "")[:1600], twilio_sid=sid))
    lead.last_message_at = deps.now()
    word = _keyword(body)
    s = deps.settings

    if word in STOP_WORDS:
        lead.opted_out = True
        lead.status, lead.closed_reason = "closed", "opt_out_request"
        log_event(db, "opted_out", lead, via="keyword", keyword=word)
        db.commit()
        if not s.twilio_handles_opt_out:  # otherwise Twilio sends its own confirmation
            body_out = f"You're unsubscribed from {client.name} texts. Reply START to resubscribe."
            sid = deps.sms.send(client.twilio_number, lead.phone, body_out)
            db.add(Message(lead_id=lead.id, role="system_notice", body=body_out, twilio_sid=sid))
            db.commit()
        return None

    if lead.opted_out:
        if word in START_WORDS:
            lead.opted_out = False
            lead.status, lead.closed_reason = "active", None
            log_event(db, "opted_in", lead, keyword=word)
            db.commit()
            if not s.twilio_handles_opt_out:
                send_sms(db, deps, client, lead, f"You're resubscribed to {client.name} texts.", "system_notice")
        else:
            db.commit()
        return None  # never auto-reply to an opted-out number

    if word in HELP_WORDS:
        db.commit()
        if not s.twilio_handles_opt_out:
            phone_line = client.config.get("business_phone", "")
            send_sms(db, deps, client, lead,
                     f"{client.name}: text here for scheduling help{', or call ' + phone_line if phone_line else ''}. "
                     f"Reply STOP to opt out.", "system_notice")
        return None

    if lead.paused:
        db.commit()
        _alert(db, deps, client, lead, "New text from a lead you're handling", f'They wrote: "{body}"')
        return None

    db.commit()
    return lead.id


def handle_missed_call(db: Session, deps: Deps, client: Client, caller: str) -> Optional[int]:
    phone = normalize_phone(caller)
    if not phone:  # blocked / anonymous caller ID: nothing to text
        log_event(db, "missed_call_no_caller_id", client=client, caller=caller)
        db.commit()
        return None
    lead = get_or_create_lead(db, client, phone, "missed_call")
    log_event(db, "missed_call", lead)
    if lead.opted_out:
        db.commit()
        _alert(db, deps, client, lead, "Missed call from a number that opted out of texts",
               "They can't be texted. Please call them back.")
        return None
    if lead.paused:
        db.commit()
        _alert(db, deps, client, lead, "Missed call from a lead you're handling", "Please call them back.")
        return None
    if _is_recent(lead, deps) and lead.messages:
        db.add(Message(lead_id=lead.id, role="event",
                       body="[EVENT] They just called the business and nobody answered. Continue the conversation."))
    else:
        _start_new_conversation(db, lead, "missed_call",
                                f"This person just called {client.name} and nobody answered. We only know "
                                f"their phone number. Write the opening text.")
    lead.last_message_at = deps.now()
    db.commit()
    return lead.id


def handle_form_lead(db: Session, deps: Deps, client: Client, payload: dict) -> tuple[Optional[int], str]:
    """payload: name, phone, email, message, zip, sms_consent. Returns (lead_id_to_reply, status)."""
    phone = normalize_phone(str(payload.get("phone", "")))
    if not phone:
        return None, "invalid_phone"
    lead = get_or_create_lead(db, client, phone, "web_form")
    name = str(payload.get("name") or "").strip()[:100]
    if name:
        lead.full_name = name
    email = str(payload.get("email") or "").strip()[:200]
    if email and "@" in email:
        lead.email = email
    zip_code = re.sub(r"\D", "", str(payload.get("zip") or ""))[:5]
    if len(zip_code) == 5 and zip_code != lead.zip_code:
        lead.zip_code, lead.in_service_area = zip_code, None
    lead.form_payload = {k: str(v)[:1000] for k, v in payload.items() if k != "phone"}
    consent = str(payload.get("sms_consent", "")).lower() in ("true", "1", "yes", "on")
    log_event(db, "form_lead", lead, consent=consent)

    if not consent:
        db.commit()
        _alert(db, deps, client, lead, "New quote request (no texting consent)",
               f"They didn't agree to texts, so the assistant won't text them. Please call.\n"
               f"Message: {payload.get('message', '')}")
        return None, "no_consent"
    if lead.opted_out:
        db.commit()
        _alert(db, deps, client, lead, "New quote request from a number that opted out of texts",
               f"Please call them.\nMessage: {payload.get('message', '')}")
        return None, "opted_out"
    if lead.paused:
        db.commit()
        _alert(db, deps, client, lead, "New form submission from a lead you're handling",
               f"Message: {payload.get('message', '')}")
        return None, "paused"

    _start_new_conversation(db, lead, "web_form",
                            "New website quote form submission. Their details are in the lead context. "
                            "Write the opening text.")
    lead.last_message_at = deps.now()
    db.commit()
    return lead.id, "queued"


def staff_reply(db: Session, deps: Deps, lead: Lead, body: str) -> bool:
    """A team member texts the lead from the inbox. The agent stops for this lead."""
    lead.paused, lead.paused_reason = True, "staff_reply"
    if lead.status == "active":
        lead.status = "escalated"
    db.commit()
    return send_sms(db, deps, lead.client, lead, body.strip()[:1600], role="staff")
