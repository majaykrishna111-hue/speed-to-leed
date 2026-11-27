"""HTTP layer: Twilio + website-form webhooks and a small staff inbox.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from . import flows
from .adapters import AlertSender, Deps, TwilioSMS, llm_key_name, make_llm
from .agent import run_agent
from .calendar_slots import GoogleCalendar
from .config import Settings, get_settings
from .models import Client, Lead, as_utc, make_session_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("speed_to_lead")

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def default_deps(settings: Settings) -> Deps:
    key_name, key_value = llm_key_name(settings)
    required = {key_name: key_value,
                "TWILIO_ACCOUNT_SID": settings.twilio_account_sid,
                "TWILIO_AUTH_TOKEN": settings.twilio_auth_token,
                "GOOGLE_SERVICE_ACCOUNT_JSON_B64": settings.google_service_account_json_b64}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return Deps(
        llm=make_llm(settings),
        sms=TwilioSMS(settings.twilio_account_sid, settings.twilio_auth_token),
        alerts=AlertSender(settings),
        calendar=GoogleCalendar(settings.google_service_account_json_b64),
        settings=settings,
        now=lambda: datetime.now(timezone.utc),
    )


def twiml(inner: str = "") -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
                    media_type="application/xml")


def create_app(deps: Optional[Deps] = None, session_factory=None) -> FastAPI:
    settings = deps.settings if deps else get_settings()
    app = FastAPI(title="Speed-to-Lead SMS Booking Agent")
    app.state.deps = deps
    app.state.sf = session_factory or make_session_factory(settings.database_url)

    def get_deps() -> Deps:
        if app.state.deps is None:
            app.state.deps = default_deps(settings)
        return app.state.deps

    async def twilio_form(request: Request) -> dict:
        form = dict(await request.form())
        if settings.validate_twilio_signature:
            from twilio.request_validator import RequestValidator
            url = settings.public_base_url.rstrip("/") + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            sig = request.headers.get("X-Twilio-Signature", "")
            if not RequestValidator(settings.twilio_auth_token).validate(url, form, sig):
                raise HTTPException(status_code=403, detail="Bad Twilio signature")
        return form

    def client_by_number(db, number: str) -> Optional[Client]:
        phone = flows.normalize_phone(number) or number
        return db.scalar(select(Client).where(Client.twilio_number == phone, Client.active.is_(True)))

    # ---------------- Twilio: SMS ----------------

    @app.post("/webhooks/twilio/sms")
    async def inbound_sms(request: Request, background: BackgroundTasks):
        form = await twilio_form(request)
        with app.state.sf() as db:
            client = client_by_number(db, form.get("To", ""))
            if client is None:
                return twiml()
            lead_id = flows.handle_inbound_sms(db, get_deps(), client, form.get("From", ""),
                                               form.get("Body", ""), form.get("MessageSid"))
        if lead_id:
            background.add_task(run_agent, app.state.sf, lead_id, get_deps())
        return twiml()

    # ---------------- Twilio: calls ----------------

    @app.post("/webhooks/twilio/voice")
    async def inbound_call(request: Request, background: BackgroundTasks):
        form = await twilio_form(request)
        with app.state.sf() as db:
            client = client_by_number(db, form.get("To", ""))
            if client is None:
                return twiml("<Hangup/>")
            cfg = client.config
            greeting = xml_escape(cfg.get("missed_call_greeting",
                                          f"Thanks for calling {client.name}. Sorry we can't pick up right now. "
                                          f"We're texting you now so we can help."))
            if cfg.get("call_mode", "missed_only") == "forward_first" and cfg.get("forward_to_number"):
                # Ring the office first; Twilio calls voice-status when the dial ends.
                return twiml(f'<Dial timeout="{int(cfg.get("ring_seconds", 18))}" '
                             f'action="/webhooks/twilio/voice-status" method="POST">'
                             f'{xml_escape(cfg["forward_to_number"])}</Dial>')
            # missed_only: the office's own phone already forwarded an unanswered call here.
            lead_id = flows.handle_missed_call(db, get_deps(), client, form.get("From", ""))
        if lead_id:
            background.add_task(run_agent, app.state.sf, lead_id, get_deps())
        return twiml(f'<Say>{greeting}</Say><Hangup/>')

    @app.post("/webhooks/twilio/voice-status")
    async def dial_status(request: Request, background: BackgroundTasks):
        form = await twilio_form(request)
        if form.get("DialCallStatus") not in MISSED_STATUSES:
            return twiml()  # answered: nothing to do
        with app.state.sf() as db:
            client = client_by_number(db, form.get("To", ""))
            if client is None:
                return twiml("<Hangup/>")
            greeting = xml_escape(client.config.get("missed_call_greeting",
                                                    f"Sorry we missed you. We're texting you now so we can help."))
            lead_id = flows.handle_missed_call(db, get_deps(), client, form.get("From", ""))
        if lead_id:
            background.add_task(run_agent, app.state.sf, lead_id, get_deps())
        return twiml(f"<Say>{greeting}</Say><Hangup/>")

    # ---------------- Website quote form ----------------

    @app.post("/webhooks/form/{client_slug}")
    async def form_lead(client_slug: str, request: Request, background: BackgroundTasks):
        ctype = request.headers.get("content-type", "")
        payload = await request.json() if "json" in ctype else dict(await request.form())
        key = request.headers.get("X-Form-Key") or request.query_params.get("key", "")
        with app.state.sf() as db:
            client = db.scalar(select(Client).where(Client.slug == client_slug, Client.active.is_(True)))
            if client is None:
                raise HTTPException(status_code=404, detail="Unknown client")
            secret = client.config.get("form_secret", "")
            if not secret or not hmac.compare_digest(str(key), secret):
                raise HTTPException(status_code=403, detail="Bad form key")
            if payload.get("website"):  # honeypot field: bots fill it, people don't see it
                return {"ok": True}
            lead_id, status = flows.handle_form_lead(db, get_deps(), client, payload)
        if lead_id:
            background.add_task(run_agent, app.state.sf, lead_id, get_deps())
        code = 422 if status == "invalid_phone" else 200
        return JSONResponse({"ok": code == 200, "status": status}, status_code=code)

    # ---------------- Staff inbox (login required) ----------------
    from .staff import install_staff
    install_staff(app, settings, get_deps)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        if not settings.enable_demo:
            raise HTTPException(404)
        query = ("?" + request.url.query) if request.url.query else ""
        return RedirectResponse("/demo" + query, status_code=307)

    # Live demo (/demo): reuses the test LLM in tests, otherwise the provider from LLM_PROVIDER.
    from .demo import DemoManager, build_demo_router
    def real_llm():
        key_name, key_value = llm_key_name(settings)
        if not key_value:
            raise RuntimeError(f"Set {key_name} in your .env file, then restart the server.")
        return make_llm(settings)

    llm_factory = (lambda: deps.llm) if deps else real_llm
    app.state.demo = DemoManager(settings, llm_factory)
    app.include_router(build_demo_router(app.state.demo))

    return app


# Module-level app for `uvicorn app.main:app`.
app = create_app()
