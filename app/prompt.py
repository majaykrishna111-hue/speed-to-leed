"""Builds the system prompt and message history sent to Claude."""
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Client, Lead, Message, SlotOffer, as_utc

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DYNAMIC_MARKER = "<current_context>"

# Client config keys that fill the fixed part of the prompt.
STATIC_FIELDS = {
    "AGENT_NAME": "agent_name", "BUSINESS_TYPE": "business_type",
    "SERVICE_AREA_DESCRIPTION": "service_area_description", "SERVICES": "services",
    "HOURS": "hours", "APPOINTMENT_TYPE": "appointment_type",
    "EMERGENCY_POLICY": "emergency_policy", "FAQ": "faq",
    "QUALIFICATION_1": "qualification_1", "QUALIFICATION_3": "qualification_3",
}


@lru_cache
def load_template() -> str:
    text = (PROMPTS_DIR / "system_prompt.md").read_text()
    parts = re.split(r"^=====\s*$", text, flags=re.MULTILINE)
    if len(parts) < 3:
        raise ValueError("system_prompt.md must contain the prompt between two '=====' lines")
    return parts[1].strip()


@lru_cache
def load_tools() -> list[dict]:
    tools = json.loads((PROMPTS_DIR / "tools.json").read_text())
    # Cache the tools together with the fixed part of the system prompt.
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _fill(template: str, values: dict[str, str]) -> str:
    def sub(m):
        key = m.group(1)
        if key not in values:
            raise KeyError(f"Missing prompt value: {key}")
        return values[key]
    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", sub, template)


def lead_context(lead: Lead, offers: list[SlotOffer], tz: str) -> str:
    lines = []
    if lead.full_name:
        lines.append(f"Name: {lead.full_name}")
    if lead.email:
        lines.append(f"Email: {lead.email}")
    if lead.service_type:
        lines.append(f"Service type: {lead.service_type}")
    if lead.issue_summary:
        lines.append(f"Issue: {lead.issue_summary}")
    if lead.zip_code:
        area = {True: "in service area", False: "OUTSIDE service area", None: "not yet checked"}
        lines.append(f"ZIP: {lead.zip_code} ({area[lead.in_service_area]})")
    if lead.urgency:
        lines.append(f"Urgency: {lead.urgency}")
    if lead.form_payload and lead.form_payload.get("message"):
        # Form text is untrusted customer input: quoted, never treated as instructions.
        lines.append(f'Form message (customer wrote): "{lead.form_payload["message"][:500]}"')
    if lead.status == "booked" and lead.appointment_start:
        when = as_utc(lead.appointment_start).astimezone(ZoneInfo(tz))
        lines.append(f"BOOKED: appointment on {when:%a %b} {when.day} at {when:%I:%M %p}")
    open_offers = [o for o in offers if not o.used]
    if open_offers and lead.status != "booked":
        lines.append("Slots already offered (book with these slot_ids):")
        lines += [f"  - slot_id {o.id}: {o.label}" for o in open_offers[-6:]]
    return "\n".join(lines) if lines else "Nothing yet beyond their phone number."


def build_system(client: Client, lead: Lead, offers: list[SlotOffer], now: datetime) -> list[dict]:
    cfg = client.config
    values = {k: str(cfg.get(v, "")) for k, v in STATIC_FIELDS.items()}
    values.update(BUSINESS_NAME=client.name, TIMEZONE=client.timezone)
    local_now = now.astimezone(ZoneInfo(client.timezone))
    values.update(
        CURRENT_DATETIME=f"{local_now:%A, %B} {local_now.day}, {local_now:%Y, %I:%M %p}",
        LEAD_SOURCE=lead.source,
        LEAD_CONTEXT=lead_context(lead, offers, client.timezone),
    )
    template = load_template()
    idx = template.index(DYNAMIC_MARKER)
    return [
        {"type": "text", "text": _fill(template[:idx], values).strip(),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _fill(template[idx:], values)},
    ]


def build_history(messages: list[Message], max_messages: int = 40) -> list[dict]:
    """Stored messages -> Claude turns. Lead texts and events are 'user'; agent, staff and
    system notices are 'assistant'. Consecutive same-role turns are merged."""
    turns: list[dict] = []
    for m in messages[-max_messages:]:
        if m.role in ("lead", "event"):
            role = "user"
            text = m.body if m.role == "lead" else m.body
        else:
            role = "assistant"
            text = f"(Sent by a team member) {m.body}" if m.role == "staff" else m.body
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns
