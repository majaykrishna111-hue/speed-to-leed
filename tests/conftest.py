"""Test harness: real app + real agent code, with Claude replaced by a scripted fake that plays
back tool calls and texts. Nothing here calls Twilio, Google or Anthropic."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient  # noqa: E402

from app.adapters import Deps, FakeAlerts, FakeSMS  # noqa: E402
from app.calendar_slots import FakeCalendar  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Client, Lead, make_session_factory  # noqa: E402

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 10, 14, 14, tzinfo=TZ)  # Friday afternoon
LEAD_PHONE = "+19195550142"
BUSINESS_NUMBER = "+19195550100"
STAFF_PASSWORD = "correct horse battery"


def tool(name, **inp):
    return ("tool", name, inp)


def text(t):
    return ("text", t)


class ScriptedLLM:
    """Each create() call consumes one step. A step is a list of ('tool', name, input) entries,
    a ('text', str) entry, an Exception to raise, or a callable(messages) returning a step."""

    def __init__(self):
        self.steps, self.calls = [], []

    def script(self, *steps):
        self.steps.extend(steps)

    def create(self, system, tools, messages, timeout, max_tokens=600):
        self.calls.append({"system": system, "messages": json.loads(json.dumps(messages, default=str))})
        if not self.steps:
            raise AssertionError("ScriptedLLM ran out of steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, Exception):
            raise step
        entries = step if isinstance(step, list) else [step]
        content, has_tool = [], False
        for i, e in enumerate(entries):
            if e[0] == "text":
                content.append(SimpleNamespace(type="text", text=e[1]))
            else:
                has_tool = True
                content.append(SimpleNamespace(type="tool_use", id=f"tu_{len(self.calls)}_{i}",
                                               name=e[1], input=e[2]))
        return SimpleNamespace(stop_reason="tool_use" if has_tool else "end_turn", content=content)


def last_tool_result(messages) -> dict:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_result":
                    return json.loads(block["content"])
    raise AssertionError("no tool result")


def make_test_db():
    """SQLite in memory by default. Set TEST_DATABASE_URL to run the suite on real Postgres."""
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    if url.startswith("sqlite"):
        return make_session_factory(url)
    from sqlalchemy import create_engine
    from app.models import Base
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return make_session_factory(url)


@pytest.fixture
def env():
    settings = Settings(database_url="sqlite://", public_base_url="https://agent.example.com",
                        session_secret="test-session-secret", anthropic_api_key="test", twilio_auth_token="tw-secret",
                        validate_twilio_signature=False, twilio_handles_opt_out=True)
    clock = {"now": NOW}
    llm, sms, alerts, cal = ScriptedLLM(), FakeSMS(), FakeAlerts(), FakeCalendar()
    deps = Deps(llm=llm, sms=sms, alerts=alerts, calendar=cal, settings=settings,
                now=lambda: clock["now"].astimezone(ZoneInfo("UTC")))
    sf = make_test_db()
    data = json.loads((ROOT / "clients" / "triangle_comfort.json").read_text())
    data["config"]["form_secret"] = "form-secret"
    with sf() as db:
        db.add(Client(slug=data["slug"], name=data["name"], twilio_number=BUSINESS_NUMBER,
                      timezone=data["timezone"], calendar_id="cal", config=data["config"]))
        db.commit()
    app = create_app(deps=deps, session_factory=sf)
    http = TestClient(app, base_url="https://testserver")  # HTTPS so Secure cookies flow

    def lead():
        with sf() as db:
            return db.query(Lead).filter_by(phone=LEAD_PHONE).first()

    def lead_texts(body):
        return http.post("/webhooks/twilio/sms", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER,
                                                       "Body": body, "MessageSid": "SM_in"})

    def submit_form(**extra):
        payload = {"name": "Jordan Lee", "phone": "(919) 555-0142", "email": "jordan@example.com",
                   "message": "AC running but blowing warm air", "zip": "", "sms_consent": True, **extra}
        return http.post("/webhooks/form/triangle-comfort", json=payload, headers={"X-Form-Key": "form-secret"})

    def add_user(email="owner@example.com", client_slug="triangle-comfort", password=STAFF_PASSWORD):
        from app.models import Client as C, StaffUser
        from app.staff import hash_password
        with sf() as db:
            cid = None if client_slug is None else db.query(C).filter_by(slug=client_slug).one().id
            db.add(StaffUser(email=email, password_hash=hash_password(password), client_id=cid))
            db.commit()

    def login(email="owner@example.com", password=STAFF_PASSWORD, client=None):
        c = client or http
        token = csrf_from(c.get("/admin/login").text)
        return c.post("/admin/login", data={"email": email, "password": password, "csrf": token,
                                            "next": "/admin"}, follow_redirects=False)

    def csrf(path="/admin", client=None):
        return csrf_from((client or http).get(path).text)

    return SimpleNamespace(settings=settings, clock=clock, llm=llm, sms=sms, alerts=alerts, cal=cal,
                           deps=deps, sf=sf, http=http, lead=lead, lead_texts=lead_texts,
                           submit_form=submit_form, add_user=add_user, login=login, csrf=csrf, app=app)


def csrf_from(page_html: str) -> str:
    import re
    m = re.search(r"name='csrf' value='([^']+)'", page_html)
    assert m, "no CSRF field on page"
    return m.group(1)
