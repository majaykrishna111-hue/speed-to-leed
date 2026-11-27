"""Staff inbox with real logins.

- Each business owner gets an account that only sees their own leads; operator accounts
  (client_id empty) see every business.
- Passwords are hashed with scrypt (Python standard library); sessions are signed cookies
  (HttpOnly, SameSite=Lax, Secure on HTTPS) that are invalidated when the password changes.
- Every form has a CSRF token, failed logins are throttled, and alert links carry no secrets.

Create accounts with:  python scripts/create_user.py owner@example.com --client triangle-comfort
"""
import base64
import hashlib
import hmac
import html
import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import flows
from .models import Client, Lead, StaffUser, as_utc
from .tool_handlers import log_event

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 10
SESSION_DAYS = 7
MAX_FAILURES = 8          # per email + IP
MAX_FAILURES_PER_IP = 30
FAILURE_WINDOW = 15 * 60  # seconds


# ---------------- Passwords ----------------

def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def _password_version(user: StaffUser) -> str:
    """Changes whenever the password changes, which logs out every existing session."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


# ---------------- Login throttling ----------------

class LoginThrottle:
    def __init__(self):
        self.failures: dict[str, deque] = defaultdict(deque)

    def _count(self, key: str) -> int:
        q, cutoff = self.failures[key], time.monotonic() - FAILURE_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return len(q)

    def blocked(self, ip: str, email: str) -> bool:
        return (self._count(f"{ip}|{email}") >= MAX_FAILURES
                or self._count(f"{ip}|*") >= MAX_FAILURES_PER_IP)

    def fail(self, ip: str, email: str) -> None:
        now = time.monotonic()
        self.failures[f"{ip}|{email}"].append(now)
        self.failures[f"{ip}|*"].append(now)

    def clear(self, ip: str, email: str) -> None:
        self.failures.pop(f"{ip}|{email}", None)


# ---------------- Pages ----------------

STYLE = """<style>
:root{color-scheme:light}
body{font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:780px;margin:0 auto;padding:16px;color:#14202b;background:#f6f8fa}
a{color:#0b5cad}h1{font-size:24px;margin:8px 0 12px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #dde3ea;border-radius:12px;padding:16px;margin:12px 0}
table{width:100%;border-collapse:collapse}td,th{padding:10px 8px;border-bottom:1px solid #e6ebf0;text-align:left;vertical-align:top}
.m{padding:10px 12px;border-radius:12px;margin:8px 0;max-width:85%;white-space:pre-wrap;overflow-wrap:anywhere}
.lead{background:#eef1f5}.agent{background:#d9ecff;margin-left:auto}.staff{background:#d7f5df;margin-left:auto}
.event,.system_notice{color:#5b6773;font-size:14px;background:none;padding:2px 0}
.tag{font-size:13px;color:#5b6773}.pill{display:inline-block;font-size:13px;font-weight:600;padding:2px 10px;border-radius:999px;background:#eef1f5}
label{display:block;font-weight:600;margin-top:10px}input,textarea{width:100%;box-sizing:border-box;font:16px system-ui;padding:10px;border:1px solid #c5ced8;border-radius:8px}
textarea{min-height:90px}button{font:600 16px system-ui;padding:10px 16px;min-height:44px;margin:8px 8px 0 0;border-radius:8px;border:1px solid #c5ced8;background:#fff;cursor:pointer}
button.primary{background:#0b5cad;color:#fff;border-color:#0b5cad}.err{color:#a4161a;font-weight:600}
form.inline{display:inline}
</style>"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    resp = HTMLResponse(f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
                        f"<title>{html.escape(title)}</title>{STYLE}</head><body>{body}</body></html>",
                        status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


class LoginRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


def safe_next(path: Optional[str]) -> str:
    if path and path.startswith("/admin") and not path.startswith("//") and "\\" not in path:
        return path
    return "/admin"


def install_staff(app: FastAPI, settings, get_deps) -> None:
    secret = settings.session_secret
    if not secret:
        secret = secrets.token_urlsafe(48)
        log.warning("SESSION_SECRET is not set: using a temporary one, so staff are logged out on restart.")
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="stl_session",
                       max_age=SESSION_DAYS * 86400, same_site="lax",
                       https_only=settings.public_base_url.startswith("https://"))
    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/admin/login?next={exc.next_path}", status_code=303)

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            request.session["csrf"] = token
        return token

    def check_csrf(request: Request, token: str) -> None:
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(str(token), expected):
            raise HTTPException(status_code=403, detail="Form expired. Go back, reload the page and try again.")

    def current_user(request: Request, db) -> StaffUser:
        uid = request.session.get("uid")
        user = db.get(StaffUser, uid) if uid else None
        if user is None or not user.active or request.session.get("pv") != _password_version(user):
            request.session.pop("uid", None)
            raise LoginRequired(safe_next(request.url.path))
        return user

    def visible_lead(db, user: StaffUser, lead_id: int) -> Lead:
        lead = db.get(Lead, lead_id)
        if lead is None or (user.client_id is not None and lead.client_id != user.client_id):
            raise HTTPException(status_code=404, detail="Not found")  # never reveal other businesses' leads
        return lead

    def hidden_csrf(request: Request) -> str:
        return f"<input type='hidden' name='csrf' value='{html.escape(csrf_token(request))}'>"

    def header(request: Request, user: StaffUser) -> str:
        who = html.escape(user.name or user.email)
        return (f"<div class='top'><div><a href='/admin'>All leads</a></div><div class='tag'>{who} · "
                f"<form class='inline' method='post' action='/admin/logout'>{hidden_csrf(request)}"
                f"<button type='submit'>Log out</button></form></div></div>")

    # ---------- login / logout ----------

    def login_form(request: Request, next_path: str, error: str = "", status: int = 200) -> HTMLResponse:
        err = f"<p class='err' role='alert'>{html.escape(error)}</p>" if error else ""
        return page("Staff login", f"""
<div class='card' style='max-width:420px;margin:48px auto'>
  <h1>Staff login</h1>{err}
  <form method='post' action='/admin/login'>
    {hidden_csrf(request)}<input type='hidden' name='next' value='{html.escape(safe_next(next_path))}'>
    <label for='email'>Email</label><input id='email' name='email' type='email' autocomplete='username' required>
    <label for='password'>Password</label><input id='password' name='password' type='password' autocomplete='current-password' required>
    <button class='primary' type='submit'>Log in</button>
  </form>
</div>""", status)

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/admin"):
        return login_form(request, next)

    @app.post("/admin/login", response_class=HTMLResponse)
    def login(request: Request, email: str = Form(...), password: str = Form(...),
              csrf: str = Form(""), next: str = Form("/admin")):
        check_csrf(request, csrf)
        ip = request.client.host if request.client else "?"
        email_key = email.strip().lower()
        if throttle.blocked(ip, email_key):
            return login_form(request, next, "Too many failed attempts. Wait 15 minutes and try again.", 429)
        with app.state.sf() as db:
            user = db.scalar(select(StaffUser).where(StaffUser.email == email_key))
            ok = bool(user and user.active and verify_password(password, user.password_hash))
            if not ok:
                if user is None:
                    verify_password(password, hash_password("timing-equalizer-only"))  # same cost either way
                throttle.fail(ip, email_key)
                log_event(db, "staff_login_failed", None, None, email=email_key, ip=ip)
                db.commit()
                return login_form(request, next, "Wrong email or password.", 401)
            throttle.clear(ip, email_key)
            user.last_login_at = datetime.now(timezone.utc)
            log_event(db, "staff_login", None, None, user_id=user.id, ip=ip)
            db.commit()
            request.session.clear()  # fresh session: prevents session fixation
            request.session.update({"uid": user.id, "pv": _password_version(user),
                                    "csrf": secrets.token_urlsafe(32)})
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/admin/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=303)

    # ---------- inbox ----------

    @app.get("/admin", response_class=HTMLResponse)
    def inbox(request: Request):
        with app.state.sf() as db:
            user = current_user(request, db)
            q = select(Lead).order_by(Lead.last_message_at.desc().nulls_last()).limit(200)
            if user.client_id is not None:
                q = q.where(Lead.client_id == user.client_id)
            leads = db.scalars(q).all()
            rows = "".join(
                f"<tr><td><a href='/admin/leads/{l.id}'>{html.escape(l.full_name or l.phone)}</a>"
                + (f"<div class='tag'>{html.escape(l.client.name)}</div>" if user.client_id is None else "")
                + f"</td><td><span class='pill'>{html.escape(l.status)}</span>"
                f"{' · paused' if l.paused else ''}{' · opted out' if l.opted_out else ''}</td>"
                f"<td>{html.escape(l.source.replace('_', ' '))}</td></tr>" for l in leads)
            empty = "<tr><td colspan='3' class='tag'>No leads yet.</td></tr>"
            return page("Leads", f"{header(request, user)}<h1>Leads</h1><div class='card'><table>"
                                 f"<tr><th>Lead</th><th>Status</th><th>Source</th></tr>{rows or empty}</table></div>")

    @app.get("/admin/leads/{lead_id}", response_class=HTMLResponse)
    def thread(request: Request, lead_id: int):
        with app.state.sf() as db:
            user = current_user(request, db)
            lead = visible_lead(db, user, lead_id)
            msgs = "".join(
                f"<div class='m {m.role}'>{'' if m.role in ('lead', 'agent', 'staff') else '[' + m.role + '] '}"
                f"{html.escape(m.body)}<div class='tag'>{m.role} · {as_utc(m.created_at):%b %d %H:%M} UTC</div></div>"
                for m in lead.messages)
            details = " · ".join(html.escape(str(x)) for x in [
                lead.phone, lead.service_type, lead.zip_code, lead.urgency, lead.status] if x)
            state = "Paused: a person is handling this lead" if lead.paused else "The assistant is handling this lead"
            action, label = (("resume", "Resume assistant") if lead.paused else ("pause", "Take over (pause assistant)"))
            name = html.escape(lead.full_name or lead.phone)
            disabled = " disabled" if lead.opted_out else ""
            note = "<p class='tag'>This person opted out of texts. You can't text them.</p>" if lead.opted_out else ""
            return page(name, f"""{header(request, user)}
<h1>{name}</h1><p class='tag'>{details}</p><p><b>{state}</b></p>
<div class='card'>{msgs or "<p class='tag'>No messages.</p>"}</div>
<div class='card'>{note}
  <form method='post' action='/admin/leads/{lead.id}/reply'>{hidden_csrf(request)}
    <label for='body'>Text the customer (this pauses the assistant)</label>
    <textarea id='body' name='body' required maxlength='1600'{disabled}></textarea>
    <button class='primary' type='submit'{disabled}>Send text</button>
  </form>
  <form method='post' action='/admin/leads/{lead.id}/{action}'>{hidden_csrf(request)}
    <button type='submit'>{label}</button></form>
</div>""")

    @app.post("/admin/leads/{lead_id}/reply")
    def reply(request: Request, lead_id: int, body: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        with app.state.sf() as db:
            user = current_user(request, db)
            lead = visible_lead(db, user, lead_id)
            log_event(db, "staff_reply", lead, user_id=user.id)
            flows.staff_reply(db, get_deps(), lead, body)
        return RedirectResponse(f"/admin/leads/{lead_id}", status_code=303)

    @app.post("/admin/leads/{lead_id}/pause")
    def pause(request: Request, lead_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        with app.state.sf() as db:
            user = current_user(request, db)
            lead = visible_lead(db, user, lead_id)
            lead.paused, lead.paused_reason = True, "manual_takeover"
            log_event(db, "staff_pause", lead, user_id=user.id)
            db.commit()
        return RedirectResponse(f"/admin/leads/{lead_id}", status_code=303)

    @app.post("/admin/leads/{lead_id}/resume")
    def resume(request: Request, lead_id: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        with app.state.sf() as db:
            user = current_user(request, db)
            lead = visible_lead(db, user, lead_id)
            lead.paused, lead.paused_reason = False, None
            if lead.status == "escalated":
                lead.status = "booked" if lead.appointment_start else "active"
            log_event(db, "staff_resume", lead, user_id=user.id)
            db.commit()
        return RedirectResponse(f"/admin/leads/{lead_id}", status_code=303)
