"""Go-live check: tests every real connection with your actual credentials, without texting anyone.

    python scripts/preflight.py                      # checks everything for every business
    python scripts/preflight.py --client triangle-comfort --write-test --slack-test

Checks:
  database    can connect; lists businesses and staff logins
  ai model    one tiny request to the model in LLM_PROVIDER
  twilio      credentials work; each business's number is in your account, can text and call,
              and its webhooks point at PUBLIC_BASE_URL
  calendar    each business calendar is shared with the service account and readable
              (--write-test also creates and deletes a 1-minute test event next week)
  slack       webhook is set (--slack-test posts a test message)
  settings    PUBLIC_BASE_URL is https, SESSION_SECRET set, demo off or protected

Run it on the production server (Render: service -> Shell) so it uses the real environment.
Exit code is 0 only if nothing failed.
"""
import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from app.adapters import llm_key_name, make_llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import Client, StaffUser, make_session_factory  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(status: str, area: str, detail: str) -> None:
    RESULTS.append((status, area, detail))
    mark = {"PASS": "  ok ", "WARN": " warn", "FAIL": " FAIL"}[status]
    print(f"[{mark}] {area}: {detail}")


def check(area: str):
    def wrap(fn):
        def run(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as exc:  # report and keep going
                record("FAIL", area, f"{type(exc).__name__}: {str(exc)[:300]}")
                if "--verbose" in sys.argv:
                    traceback.print_exc()
        return run
    return wrap


@check("settings")
def check_settings(s) -> None:
    if not s.public_base_url.startswith("https://"):
        record("FAIL", "settings", f"PUBLIC_BASE_URL must be https (is {s.public_base_url!r})")
    else:
        record("PASS", "settings", f"public URL {s.public_base_url}")
    if not s.session_secret or len(s.session_secret) < 32:
        record("FAIL", "settings", "SESSION_SECRET missing or shorter than 32 characters")
    else:
        record("PASS", "settings", "SESSION_SECRET set")
    if s.enable_demo and not s.demo_token:
        record("WARN", "settings", "demo is on and public: set ENABLE_DEMO=false on the production service")
    if not s.validate_twilio_signature:
        record("FAIL", "settings", "VALIDATE_TWILIO_SIGNATURE is off: anyone could fake texts")
    if s.llm_provider == "gemini":
        record("WARN", "settings", "LLM_PROVIDER=gemini: never use a free-tier key with real customer data")


@check("database")
def check_database(sf, only: str | None) -> list:
    with sf() as db:
        db.execute(text("select 1"))
        clients = list(db.scalars(select(Client).where(Client.active.is_(True))))
        if only:
            clients = [c for c in clients if c.slug == only]
        users = list(db.scalars(select(StaffUser).where(StaffUser.active.is_(True))))
    record("PASS", "database", f"connected; {len(clients)} active business(es), {len(users)} staff login(s)")
    if not clients:
        record("FAIL", "database", "no businesses: run scripts/upsert_client.py")
    for c in clients:
        if not any(u.client_id in (c.id, None) for u in users):
            record("WARN", "database", f"{c.slug}: nobody can log in to its inbox (scripts/create_user.py)")
    return clients


@check("ai model")
def check_llm(s) -> None:
    name, value = llm_key_name(s)
    if not value:
        record("FAIL", "ai model", f"{name} is not set")
        return
    from app.prompt import load_tools
    resp = make_llm(s).create(system=[{"type": "text", "text": "Reply with the single word OK."}],
                              tools=load_tools(), messages=[{"role": "user", "content": "Say OK"}],
                              timeout=30, max_tokens=20)
    reply = "".join(getattr(b, "text", "") or "" for b in resp.content).strip()
    record("PASS", "ai model", f"{s.llm_provider} answered: {reply[:40]!r}")


@check("twilio")
def check_twilio(s, clients) -> None:
    if not (s.twilio_account_sid and s.twilio_auth_token):
        record("FAIL", "twilio", "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
        return
    from twilio.rest import Client as Twilio
    tw = Twilio(s.twilio_account_sid, s.twilio_auth_token)
    account = tw.api.v2010.accounts(s.twilio_account_sid).fetch()
    record("PASS", "twilio", f"credentials work (account status: {account.status})")
    if account.type == "Trial":
        record("WARN", "twilio", "trial account: can only text verified numbers, and adds a trial prefix")
    base = s.public_base_url.rstrip("/")
    for c in clients:
        found = tw.incoming_phone_numbers.list(phone_number=c.twilio_number, limit=1)
        if not found:
            record("FAIL", "twilio", f"{c.slug}: {c.twilio_number} is not in this Twilio account")
            continue
        n = found[0]
        caps = n.capabilities or {}
        if not caps.get("sms") or not caps.get("voice"):
            record("FAIL", "twilio", f"{c.slug}: number lacks SMS or voice capability ({caps})")
        for label, actual, path in [("messaging", n.sms_url, "/webhooks/twilio/sms"),
                                    ("voice", n.voice_url, "/webhooks/twilio/voice")]:
            want = base + path
            if n.sms_application_sid and label == "messaging":
                record("WARN", "twilio", f"{c.slug}: SMS handled by a Messaging Service; make sure its "
                                         f"inbound webhook is {want}")
            elif (actual or "").rstrip("/") != want:
                record("FAIL", "twilio", f"{c.slug}: {label} webhook is {actual!r}, should be {want!r}")
            else:
                record("PASS", "twilio", f"{c.slug}: {label} webhook ok")


@check("calendar")
def check_calendar(s, clients, write_test: bool) -> None:
    if not s.google_service_account_json_b64:
        record("FAIL", "calendar", "GOOGLE_SERVICE_ACCOUNT_JSON_B64 not set")
        return
    from zoneinfo import ZoneInfo
    from app.calendar_slots import GoogleCalendar
    cal = GoogleCalendar(s.google_service_account_json_b64)
    record("PASS", "calendar", f"service account {cal.service_account_email}")
    for c in clients:
        if not c.calendar_id or "REPLACE" in c.calendar_id:
            record("FAIL", "calendar", f"{c.slug}: calendar_id not set")
            continue
        now = datetime.now(ZoneInfo(c.timezone))
        try:
            busy = cal.busy_intervals(c.calendar_id, now, now + timedelta(days=7))
        except Exception as exc:
            record("FAIL", "calendar", f"{c.slug}: can't read the calendar. Is it shared with "
                                       f"{cal.service_account_email} ('Make changes to events')? ({str(exc)[:150]})")
            continue
        record("PASS", "calendar", f"{c.slug}: readable, {len(busy)} busy block(s) in the next 7 days")
        if write_test:
            start = (now + timedelta(days=7)).replace(hour=23, minute=30, second=0, microsecond=0)
            event_id = cal.create_event(c.calendar_id, start, start + timedelta(minutes=1),
                                        "Speed-to-Lead preflight test (safe to delete)", "", c.timezone)
            cal.delete_event(c.calendar_id, event_id)
            record("PASS", "calendar", f"{c.slug}: created and deleted a test event")


@check("slack")
def check_slack(clients, send_test: bool) -> None:
    import httpx
    for c in clients:
        url = c.config.get("slack_webhook_url")
        if not url and not c.config.get("owner_email"):
            record("FAIL", "slack", f"{c.slug}: no slack_webhook_url or owner_email: alerts go nowhere")
            continue
        if url and send_test:
            r = httpx.post(url, json={"text": "Speed-to-Lead preflight: alerts are working."}, timeout=10)
            record("PASS" if r.status_code == 200 else "FAIL", "slack",
                   f"{c.slug}: test message -> HTTP {r.status_code}")
        elif url:
            record("PASS", "slack", f"{c.slug}: webhook set (add --slack-test to send a test message)")
        else:
            record("WARN", "slack", f"{c.slug}: email alerts only")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", help="only check this business slug")
    ap.add_argument("--write-test", action="store_true", help="create and delete a calendar test event")
    ap.add_argument("--slack-test", action="store_true", help="post a test message to Slack")
    ap.add_argument("--skip-ai", action="store_true", help="don't spend a model request")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    print(f"Preflight at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")
    check_settings(s)
    clients = check_database(make_session_factory(s.database_url), args.client) or []
    if not args.skip_ai:
        check_llm(s)
    check_twilio(s, clients)
    check_calendar(s, clients, args.write_test)
    check_slack(clients, args.slack_test)

    fails = sum(1 for r in RESULTS if r[0] == "FAIL")
    warns = sum(1 for r in RESULTS if r[0] == "WARN")
    print(f"\n{fails} failed, {warns} warning(s), {len(RESULTS) - fails - warns} passed.")
    print("Ready for a live test call." if not fails else "Fix the FAIL lines, then run this again.")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
