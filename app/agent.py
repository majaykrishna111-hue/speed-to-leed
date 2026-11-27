"""The agent loop: one call to run_agent() answers whatever the lead is waiting on.

Guardrails enforced here in code (the prompt asks for them too, but code is the backstop):
- never texts opted-out or paused (human-owned) leads
- one run per lead at a time, and only when a reply is actually owed
- hard 300-character limit with one rewrite attempt, ASCII clean-up for cheap GSM-7 SMS
- daily cap on automated texts per lead
- deadline on the first text (falls back to a template so speed-to-lead always holds)
- any failure -> a safe holding text, owner alert, and the lead is paused for a human
"""
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .adapters import Deps, OptedOutError
from .models import Client, Lead, Message, as_utc
from .prompt import build_history, build_system, load_tools
from .tool_handlers import ToolContext, execute_tool, inbox_link, log_event, offers_for

log = logging.getLogger(__name__)

MAX_SMS_CHARS = 300
_LOCKS: dict[int, threading.Lock] = defaultdict(threading.Lock)
_LOCKS_GUARD = threading.Lock()

ASCII_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                           "–": "-", "—": "-", "…": "...", " ": " "})
LEAK_MARKERS = ("slot_id", "no_reply", "{{", "tool_use", "system prompt", "system note", "<", ">")
SLOT_ID_RE = re.compile(r"\bs_[0-9a-f]{6}\b")


class DeadlineExceeded(Exception):
    pass


class BadReply(Exception):
    pass


def _lock_for(lead_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS[lead_id]


def sanitize(text: str) -> str:
    text = (text or "").translate(ASCII_MAP)
    text = re.sub(r"\*+|`+|__", "", text)              # stray markdown (keeps single _ for emails)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def problem_with(text: str) -> Optional[str]:
    if not text:
        return "The text was empty."
    if text == "NO_REPLY":
        return None
    if len(text) > MAX_SMS_CHARS:
        return f"That text is {len(text)} characters; the limit is {MAX_SMS_CHARS}."
    low = text.lower()
    if any(marker in low for marker in LEAK_MARKERS) or SLOT_ID_RE.search(text):
        return "That text contains internal details (IDs, markup or system words)."
    return None


def needs_reply(messages: list[Message]) -> bool:
    for m in reversed(messages):
        if m.role in ("lead", "event"):
            return True
        if m.role in ("agent", "staff", "system_notice"):
            return False
    return False


def send_sms(db: Session, deps: Deps, client: Client, lead: Lead, body: str, role: str = "agent") -> bool:
    if lead.opted_out:
        return False
    try:
        sid = deps.sms.send(client.twilio_number, lead.phone, body)
    except OptedOutError:
        lead.opted_out = True
        log_event(db, "opted_out_at_carrier", lead)
        db.commit()
        return False
    db.add(Message(lead_id=lead.id, role=role, body=body, twilio_sid=sid))
    lead.last_message_at = deps.now()
    db.commit()
    db.refresh(lead)
    return True


def template_opener(client: Client, lead: Lead) -> str:
    agent = client.config.get("agent_name", "the scheduling assistant")
    first = (lead.full_name or "").split(" ")[0]
    hello = f"Hi {first}" if first else "Hi"
    if lead.source == "missed_call":
        body = (f"{hello}, this is {agent} with {client.name}. Sorry we missed your call! "
                f"I can help by text. What can we help you with?")
    else:
        body = f"{hello}, this is {agent} with {client.name}. Thanks for reaching out! What can we help you with?"
    return f"{body} Reply STOP to opt out."


def run_agent(session_factory, lead_id: int, deps: Deps) -> None:
    """Entry point used by the webhooks (runs in a background thread)."""
    with _lock_for(lead_id):
        with session_factory() as db:
            try:
                _run(db, lead_id, deps)
            except Exception:
                log.exception("run_agent crashed for lead %s", lead_id)
                db.rollback()


def _run(db: Session, lead_id: int, deps: Deps) -> None:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.opted_out or lead.paused:
        return
    client = lead.client
    if not client.active or not needs_reply(lead.messages):
        return
    if lead.status == "closed":
        if lead.closed_reason == "opt_out_request":
            return
        lead.status, lead.closed_reason = "active", None  # they came back; pick it up again

    settings = deps.settings
    since = deps.now() - timedelta(hours=24)
    sent_today = sum(1 for m in lead.messages if m.role == "agent" and as_utc(m.created_at) >= since)
    if sent_today >= settings.max_agent_texts_per_day:
        _hand_to_human(db, deps, client, lead, "Daily automated-text limit reached", holding_text=None)
        return

    last = lead.messages[-1]
    is_opener = last.role == "event" and last.body.startswith("[NEW_LEAD]") and not any(
        m.role in ("agent", "staff") for m in lead.messages if m.id > last.id)
    deadline = settings.opener_deadline_seconds if is_opener else settings.reply_deadline_seconds

    ctx = ToolContext(db=db, client=client, lead=lead, deps=deps)
    try:
        reply = _converse(ctx, deadline)
    except Exception as exc:
        db.rollback()  # tool side effects were already committed round by round
        db.refresh(lead)
        log_event(db, "agent_error", lead, error=f"{type(exc).__name__}: {str(exc)[:300]}",
                  opener=is_opener)
        db.commit()
        if ctx.opt_out:
            _confirm_opt_out(db, deps, client, lead)
        elif ctx.escalated:  # owner already alerted; just hold and pause
            send_sms(db, deps, client, lead, "Thanks! A team member will reach out to you shortly.")
            lead.paused, lead.paused_reason, lead.status = True, "escalated", "escalated"
            db.commit()
        elif is_opener:
            send_sms(db, deps, client, lead, template_opener(client, lead))
        elif ctx.booked_label:
            send_sms(db, deps, client, lead,
                     f"You're all set for {ctx.booked_label}. {client.config.get('what_to_expect', '')}".strip())
        else:
            _hand_to_human(db, deps, client, lead, f"Agent error ({type(exc).__name__})",
                           holding_text="Thanks for your message! A team member will text you back shortly.")
        return

    db.commit()
    if reply and reply != "NO_REPLY":
        send_sms(db, deps, client, lead, reply)
    if ctx.opt_out:
        _confirm_opt_out(db, deps, client, lead)
    if ctx.escalated:
        lead.paused, lead.paused_reason, lead.status = True, "escalated", "escalated"
    db.commit()


def _converse(ctx: ToolContext, deadline: float) -> str:
    deps = ctx.deps
    started = time.monotonic()

    def remaining() -> float:
        left = deadline - (time.monotonic() - started)
        if left <= 0.5:
            raise DeadlineExceeded()
        return left

    system = build_system(ctx.client, ctx.lead, offers_for(ctx.db, ctx.lead), deps.now())
    messages = build_history(ctx.lead.messages)
    tools = load_tools()

    def call() -> object:
        return deps.llm.create(system=system, tools=tools, messages=messages, timeout=remaining())

    final_blocks = None
    for _ in range(deps.settings.max_tool_rounds):
        resp = call()
        blocks = [_to_param(b) for b in resp.content]
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for b in blocks:
                if b["type"] == "tool_use":
                    result, is_error = execute_tool(ctx, b["name"], b["input"])
                    results.append({"type": "tool_result", "tool_use_id": b["id"],
                                    "content": json.dumps(result, default=str), "is_error": is_error})
            messages.append({"role": "user", "content": results})
            ctx.db.commit()  # persist tool effects (bookings, saved details) immediately
            continue
        final_blocks = blocks
        break
    if final_blocks is None:
        raise BadReply("Too many tool rounds")

    text = sanitize("".join(b.get("text", "") for b in final_blocks if b["type"] == "text"))
    issue = problem_with(text)
    if issue is None:
        return text

    # One rewrite attempt.
    messages.append({"role": "assistant", "content": final_blocks or [{"type": "text", "text": "(empty)"}]})
    messages.append({"role": "user", "content": (
        f"[SYSTEM NOTE - not from the customer] {issue} Rewrite your text to the customer "
        f"in under 280 characters, same meaning, plain words only. Output only the text.")})
    resp = call()
    text = sanitize("".join(getattr(b, "text", "") or "" for b in resp.content
                            if getattr(b, "type", "") == "text"))
    issue = problem_with(text)
    if issue:
        raise BadReply(issue)
    return text


def _to_param(block) -> dict:
    """SDK content block (or test double) -> plain dict we can send back to the API."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


def _confirm_opt_out(db: Session, deps: Deps, client: Client, lead: Lead) -> None:
    """Informal opt-out ('please stop texting me'): Twilio didn't see a keyword, so we confirm."""
    send_sms(db, deps, client, lead,
             f"You're unsubscribed from {client.name} texts and won't get more messages.",
             role="system_notice")
    lead.opted_out = True
    log_event(db, "opted_out", lead, via="conversation")
    db.commit()


def _hand_to_human(db: Session, deps: Deps, client: Client, lead: Lead, reason: str,
                   holding_text: Optional[str]) -> None:
    if holding_text:
        send_sms(db, deps, client, lead, holding_text)
    lead.paused, lead.paused_reason, lead.status = True, reason[:60], "escalated"
    log_event(db, "handed_to_human", lead, reason=reason)
    db.commit()
    ctx = ToolContext(db=db, client=client, lead=lead, deps=deps)
    deps.alerts.send(client.config, f"Please take over: {lead.full_name or lead.phone}",
                     f"{reason}. Automated replies are paused.\nLead: {lead.phone}\n{inbox_link(ctx)}")
