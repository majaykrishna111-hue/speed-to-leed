"""Live demo at /demo: the real agent (prompt, tools, guardrails, LLM) with simulated texting,
calendar and owner alerts. Needs only the LLM key - no Twilio, Google Calendar or Slack.

The demo keeps its own in-memory database, separate from real leads, and never sends a real SMS.
"""
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import flows
from .adapters import Deps, FakeSMS, llm_key_name
from .agent import run_agent
from .calendar_slots import FakeCalendar, slot_label
from .models import Client, Lead, as_utc, make_session_factory

ROOT = Path(__file__).resolve().parent.parent
DEMO_HTML = Path(__file__).resolve().parent / "static" / "demo.html"
DEMO_CLIENT_FILE = ROOT / "clients" / "triangle_comfort.json"
DEMO_LEAD_PHONE = "+19195550142"
ADMIN_URL_RE = re.compile(r"https?://\S*/admin")


@dataclass
class DemoAlerts:
    """Records owner alerts with a timestamp for the live view."""
    sent: list[dict] = field(default_factory=list)

    def send(self, client_cfg, title, text, urgent=False):
        # Alert text carries an inbox link with the admin token: never show that in the demo.
        clean = "\n".join(line for line in (text or "").splitlines() if not ADMIN_URL_RE.search(line)).strip()
        self.sent.append({"title": title, "text": clean, "urgent": bool(urgent),
                          "at": datetime.now(timezone.utc).isoformat()})


class DemoSession:
    """One self-contained demo world: database, fake phone, fake calendar, alerts."""

    def __init__(self, settings, llm_factory: Callable[[], Any]):
        self.settings = settings.model_copy(update={"twilio_handles_opt_out": False})
        self.sf = make_session_factory("sqlite://")
        self.sms = FakeSMS()
        self.alerts = DemoAlerts()
        self.calendar = FakeCalendar()
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []
        self.actions = 0  # customer actions in this session (form, texts, calls)
        self.llm = None
        self.llm_error: Optional[str] = None
        try:
            self.llm = llm_factory()
        except Exception as exc:  # missing key or SDK problem: show it on the page
            self.llm_error = f"{type(exc).__name__}: {exc}"

        data = json.loads(DEMO_CLIENT_FILE.read_text())
        self.tz = ZoneInfo(data.get("timezone", "America/New_York"))
        with self.sf() as db:
            client = Client(slug="demo", name=data["name"], twilio_number=data["twilio_number"],
                            timezone=str(self.tz), calendar_id="demo", config=data["config"])
            db.add(client)
            db.commit()
            self.client_id = client.id
            self.business_name = client.name
            self.agent_name = data["config"].get("agent_name", "Ava")
            self.capacity = int(data["config"].get("slot_capacity", 1))
        self._prefill_calendar()
        self.deps = Deps(llm=self.llm, sms=self.sms, alerts=self.alerts, calendar=self.calendar,
                         settings=self.settings, now=lambda: datetime.now(timezone.utc))

    def _prefill_calendar(self) -> None:
        """Existing jobs, so the demo shows that full windows are never offered."""
        tomorrow = datetime.now(self.tz).date() + timedelta(days=1)
        start = datetime.combine(tomorrow, datetime.min.time(), self.tz).replace(hour=8)
        for _ in range(self.capacity):  # fill tomorrow 8-10am completely
            self.calendar.create_event("demo", start, start + timedelta(hours=2), "Existing job", "", str(self.tz))
        self.prefilled = len(self.calendar.events)

    # -- running the agent in the background, like the real webhooks do --

    @property
    def busy(self) -> bool:
        self.threads = [t for t in self.threads if t.is_alive()]
        return bool(self.threads)

    def run(self, lead_id: Optional[int]) -> None:
        if not lead_id:
            return
        if self.llm is None:
            self.errors.append(self.llm_error or "No LLM configured")
            return

        def work():
            try:
                run_agent(self.sf, lead_id, self.deps)
            except Exception as exc:  # run_agent already logs; surface it on the page too
                self.errors.append(f"{type(exc).__name__}: {exc}")

        t = threading.Thread(target=work, daemon=True)
        self.threads.append(t)
        t.start()

    def _agent_errors(self) -> list[str]:
        return _agent_errors_from(self.sf)

    def llm_check(self) -> dict:
        """One tiny real request to the model, to confirm the key, model and quota work."""
        if self.llm is None:
            return {"ok": False, "error": self.llm_error}
        from .prompt import load_tools
        try:
            resp = self.llm.create(system=[{"type": "text", "text": "Reply with the single word OK."}],
                                   tools=load_tools(), messages=[{"role": "user", "content": "Say OK"}],
                                   timeout=20, max_tokens=20)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:800]}"}
        text = "".join(getattr(b, "text", "") or "" for b in resp.content)
        return {"ok": True, "reply": text[:100]}

    def wait_idle(self, timeout: float = 30) -> None:
        for t in list(self.threads):
            t.join(timeout)

    def client(self, db) -> Client:
        return db.get(Client, self.client_id)

    # -- the snapshot the page polls --

    def state(self) -> dict:
        with self.sf() as db:
            lead = db.query(Lead).filter_by(phone=DEMO_LEAD_PHONE).first()
            messages, lead_info = [], None
            if lead is not None:
                for m in lead.messages:
                    if m.role == "event":
                        kind = "missed_call" if "nobody answered" in m.body else "form"
                        messages.append({"role": "event", "kind": kind, "body": "",
                                         "at": as_utc(m.created_at).isoformat()})
                        continue
                    messages.append({"role": m.role, "body": m.body, "chars": len(m.body),
                                     "at": as_utc(m.created_at).isoformat()})
                lead_info = {
                    "status": status_label(lead),
                    "raw_status": lead.status, "closed_reason": lead.closed_reason,
                    "paused_reason": lead.paused_reason,
                    "name": lead.full_name, "email": lead.email, "zip": lead.zip_code,
                    "in_area": lead.in_service_area, "service": lead.service_type,
                    "issue": lead.issue_summary, "urgency": lead.urgency, "source": lead.source,
                }
        bookings = [{
            "start": e["start"].astimezone(self.tz).isoformat(),
            "label": slot_label(e["start"].astimezone(self.tz), e["end"].astimezone(self.tz)),
            "summary": e["summary"], "description": e["description"],
        } for e in self.calendar.events[self.prefilled:]]
        provider = (self.settings.llm_provider or "gemini").lower()
        model = self.settings.gemini_model if provider == "gemini" else self.settings.anthropic_model
        return {
            "business": self.business_name, "agent": self.agent_name, "provider": provider, "model": model,
            "llm_ready": self.llm is not None, "llm_error": self.llm_error,
            "busy": self.busy, "messages": messages, "lead": lead_info, "bookings": bookings,
            "alerts": list(reversed(self.alerts.sent)), "errors": (self.errors + self._agent_errors())[-3:],
        }


def _agent_errors_from(sf) -> list[str]:
    from .models import EventLog
    with sf() as db:
        rows = db.query(EventLog).filter(EventLog.type == "agent_error").order_by(EventLog.id).all()
        return [str((r.data or {}).get("error", "unknown error")) for r in rows]


def _hour(t: datetime) -> str:
    h = t.hour % 12 or 12
    return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}{'am' if t.hour < 12 else 'pm'}"


def status_label(lead: Lead) -> str:
    if lead.opted_out:
        return "Opted Out"
    if lead.paused or lead.status == "escalated":
        return "Escalated"
    if lead.status == "booked":
        return "Booked"
    if lead.status == "closed":
        return "Closed"
    return "Active"


class FormIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    zip: str = Field(default="", max_length=10)
    issue: str = Field(min_length=1, max_length=500)


class SmsIn(BaseModel):
    body: str = Field(min_length=1, max_length=1600)


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class DemoManager:
    """One private DemoSession per browser (X-Demo-Session header), so visitors never see each
    other's conversations. Idle sessions expire; usage caps protect the LLM quota."""

    def __init__(self, settings, llm_factory: Callable[[], Any]):
        self.settings = settings
        self.llm_factory = llm_factory
        self._sessions: "OrderedDict[str, DemoSession]" = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._lock = threading.Lock()
        self._day = ""
        self._today_count = 0

    def get(self, sid: str = "default") -> DemoSession:
        with self._lock:
            now = time.monotonic()
            ttl = self.settings.demo_session_idle_minutes * 60
            for old in [k for k, t in self._last_used.items() if now - t > ttl]:
                self._sessions.pop(old, None)
                self._last_used.pop(old, None)
            if sid not in self._sessions:
                while len(self._sessions) >= self.settings.demo_max_sessions:
                    oldest, _ = self._sessions.popitem(last=False)
                    self._last_used.pop(oldest, None)
                self._sessions[sid] = DemoSession(self.settings, self.llm_factory)
            self._sessions.move_to_end(sid)
            self._last_used[sid] = now
            return self._sessions[sid]

    @property
    def session(self) -> DemoSession:  # the shared default session (used by tests)
        return self.get("default")

    def reset(self, sid: str = "default") -> DemoSession:
        with self._lock:
            self._sessions.pop(sid, None)
            self._last_used.pop(sid, None)
        return self.get(sid)

    def charge(self, session: DemoSession) -> None:
        """Count one customer action that will cost LLM requests; refuse past the caps."""
        s = self.settings
        if session.actions >= s.demo_max_actions_per_session:
            raise HTTPException(429, "This demo conversation hit its message limit. Press Reset Demo to start over.")
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._day:
                self._day, self._today_count = today, 0
            if self._today_count >= s.demo_daily_action_limit:
                raise HTTPException(429, "The demo has reached today's usage limit. Please try again tomorrow.")
            self._today_count += 1
        session.actions += 1


def build_demo_router(manager: DemoManager) -> APIRouter:
    router = APIRouter()
    s = manager.settings

    def guard(request: Request) -> None:
        if not s.enable_demo:
            raise HTTPException(404)
        if s.demo_token and request.query_params.get("token") != s.demo_token \
                and request.headers.get("X-Demo-Token") != s.demo_token:
            raise HTTPException(403, "Add ?token=YOUR_DEMO_TOKEN to the URL")

    def session_for(request: Request) -> tuple[str, DemoSession]:
        guard(request)
        sid = request.headers.get("X-Demo-Session", "default")
        if sid != "default" and not SESSION_ID_RE.match(sid):
            raise HTTPException(400, "Bad demo session id")
        return sid, manager.get(sid)

    @router.get("/demo", include_in_schema=False)
    def demo_page(request: Request):
        guard(request)
        return FileResponse(DEMO_HTML, media_type="text/html")

    @router.get("/demo/api/state")
    def demo_state(request: Request):
        return session_for(request)[1].state()

    @router.post("/demo/api/form")
    def demo_form(body: FormIn, request: Request):
        _, session = session_for(request)
        if session.busy:
            raise HTTPException(409, f"{session.agent_name} is still replying")
        manager.charge(session)
        with session.sf() as db:
            lead_id, status = flows.handle_form_lead(db, session.deps, session.client(db), {
                "name": body.name, "phone": DEMO_LEAD_PHONE, "message": body.issue,
                "zip": body.zip, "sms_consent": "true"})
        session.run(lead_id)
        return {"ok": True, "status": status}

    @router.post("/demo/api/sms")
    def demo_sms(body: SmsIn, request: Request):
        _, session = session_for(request)
        manager.charge(session)
        with session.sf() as db:
            lead_id = flows.handle_inbound_sms(db, session.deps, session.client(db), DEMO_LEAD_PHONE, body.body)
        session.run(lead_id)
        return {"ok": True, "agent_will_reply": bool(lead_id)}

    @router.post("/demo/api/missed-call")
    def demo_missed_call(request: Request):
        _, session = session_for(request)
        if session.busy:
            raise HTTPException(409, f"{session.agent_name} is still replying")
        manager.charge(session)
        with session.sf() as db:
            lead_id = flows.handle_missed_call(db, session.deps, session.client(db), DEMO_LEAD_PHONE)
        session.run(lead_id)
        return {"ok": True}

    @router.post("/demo/api/reset")
    def demo_reset(request: Request):
        sid, _ = session_for(request)
        manager.reset(sid)
        return {"ok": True}

    @router.get("/demo/api/llm-check")
    def demo_llm_check(request: Request):
        _, session = session_for(request)
        manager.charge(session)
        return session.llm_check()

    @router.get("/demo/api/health")
    def demo_health(request: Request):
        guard(request)
        key_name, key_value = llm_key_name(s)
        return {"llm_key_set": bool(key_value), "key_name": key_name}

    return router
