"""Talk to the agent in your terminal with a real LLM, before any Twilio or Google Calendar setup.
Texts, calendar and owner alerts are simulated; the conversation logic is the real production code.

Uses LLM_PROVIDER from your .env: "gemini" needs GEMINI_API_KEY (free from Google AI Studio),
"anthropic" needs ANTHROPIC_API_KEY.

Interactive:
    python scripts/simulate.py --source web_form --name "Jordan Lee" --message "AC blowing warm air"
    python scripts/simulate.py --source missed_call

Replay a test scenario:
    python scripts/simulate.py --scenario scenarios/01_happy_path.txt

While chatting: /state shows the lead record, /calendar shows bookings, /quit exits.
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from app import flows  # noqa: E402
from app.adapters import Deps, FakeAlerts, FakeSMS, llm_key_name, make_llm  # noqa: E402
from app.agent import run_agent  # noqa: E402
from app.calendar_slots import FakeCalendar  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Client, Lead, make_session_factory  # noqa: E402

LEAD_PHONE = "+19195550142"


def parse_scenario(path: str) -> tuple[dict, list[str]]:
    headers, lines = {}, []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if ":" in line:
                key, value = line[1:].split(":", 1)
                headers[key.strip().lower()] = value.strip()
            continue
        lines.append(line)
    return headers, lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="clients/triangle_comfort.json")
    ap.add_argument("--scenario")
    ap.add_argument("--source", choices=["web_form", "missed_call", "inbound_sms"], default="web_form")
    ap.add_argument("--name", default="")
    ap.add_argument("--message", default="")
    ap.add_argument("--zip", default="")
    ap.add_argument("--now", help="Pretend current local time, e.g. 2026-07-10T14:14")
    args = ap.parse_args()

    headers, script = parse_scenario(args.scenario) if args.scenario else ({}, [])
    source = headers.get("source", args.source)
    name, message = headers.get("name", args.name), headers.get("message", args.message)
    zip_code, now_s = headers.get("zip", args.zip), headers.get("now", args.now)

    settings = get_settings()
    key_name, key_value = llm_key_name(settings)
    if not key_value:
        sys.exit(f"Set {key_name} first (in your shell or .env). LLM_PROVIDER is {settings.llm_provider!r}.")
    settings.public_base_url = "http://localhost:8000"
    settings.twilio_handles_opt_out = False  # so you see the opt-out confirmation texts

    data = json.loads(Path(args.client).read_text())
    tz = ZoneInfo(data.get("timezone", "America/New_York"))
    start_local = datetime.fromisoformat(now_s).replace(tzinfo=tz) if now_s else datetime.now(tz)
    clock = {"now": start_local}

    # Pre-fill the fake calendar so some windows are already taken.
    cal = FakeCalendar()
    day = start_local.date()
    for d, hour in [(1, 8), (1, 8), (1, 13), (2, 10)]:
        s = datetime.combine(day + timedelta(days=d), datetime.min.time(), tz).replace(hour=hour)
        cal.create_event("cal", s, s + timedelta(hours=2), "Existing job", "", str(tz))

    deps = Deps(llm=make_llm(settings),
                sms=FakeSMS(echo=True), alerts=FakeAlerts(echo=True), calendar=cal,
                settings=settings, now=lambda: clock["now"].astimezone(ZoneInfo("UTC")))
    sf = make_session_factory("sqlite://")
    with sf() as db:
        client = Client(slug=data["slug"], name=data["name"], twilio_number=data["twilio_number"],
                        timezone=str(tz), calendar_id="cal", config=data["config"])
        db.add(client)
        db.commit()
        model = settings.gemini_model if settings.llm_provider == "gemini" else settings.anthropic_model
        print(f"\nSimulating {client.name} | model: {model} | lead source: {source} | "
              f"time: {start_local:%a %b %d %I:%M %p}\n")
        if source == "web_form":
            lead_id, status = flows.handle_form_lead(db, deps, client, {
                "name": name, "phone": LEAD_PHONE, "message": message, "zip": zip_code, "sms_consent": "true"})
            print(f"  [Form submitted: name={name!r}, message={message!r}, zip={zip_code!r}] -> {status}")
        elif source == "missed_call":
            lead_id = flows.handle_missed_call(db, deps, client, LEAD_PHONE)
            print("  [Missed call from", LEAD_PHONE + "]")
        else:
            lead_id = None
    if lead_id:
        run_agent(sf, lead_id, deps)

    def send(text: str) -> None:
        clock["now"] += timedelta(minutes=2)
        with sf() as db:
            client = db.query(Client).first()
            lid = flows.handle_inbound_sms(db, deps, client, LEAD_PHONE, text)
        if lid:
            run_agent(sf, lid, deps)
        else:
            with sf() as db:
                lead = db.query(Lead).first()
                state = "opted out" if lead.opted_out else "paused for a human" if lead.paused else "no reply owed"
            print(f"  [No automated reply: {state}]")

    for line in script:
        print(f"  LEAD: {line}")
        send(line)

    if script:
        show_state(sf, cal)
        return
    while True:
        try:
            text = input("YOU (as the lead): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "/quit":
            break
        if text == "/state":
            show_state(sf, cal)
            continue
        if text == "/calendar":
            for e in cal.events:
                print(f"  {e['start']:%a %b %d %I:%M %p}  {e['summary']}")
            continue
        if text:
            send(text)


def show_state(sf, cal) -> None:
    with sf() as db:
        lead = db.query(Lead).first()
        if lead is None:
            print("  (no lead)")
            return
        fields = ["status", "closed_reason", "paused", "paused_reason", "opted_out", "full_name", "email",
                  "service_type", "issue_summary", "zip_code", "in_service_area", "urgency", "appointment_start"]
        print("\n  LEAD RECORD")
        for f in fields:
            print(f"    {f}: {getattr(lead, f)}")
    booked = [e for e in cal.events if e["summary"] != "Existing job"]
    print(f"  BOOKINGS CREATED: {len(booked)}")
    for e in booked:
        print(f"    {e['start']:%a %b %d %I:%M %p}  {e['summary']}\n      " + e["description"].replace("\n", "\n      "))


if __name__ == "__main__":
    main()
