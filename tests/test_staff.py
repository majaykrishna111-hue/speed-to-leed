"""Staff login security: accounts, scoping, CSRF, throttling, sessions."""
from fastapi.testclient import TestClient

from app.models import Client, Lead, StaffUser
from app.staff import hash_password, safe_next, verify_password
from conftest import STAFF_PASSWORD, csrf_from, text


def make_other_business(env):
    with env.sf() as db:
        other = Client(slug="other-hvac", name="Other HVAC Co", twilio_number="+19195550199",
                       timezone="America/New_York", config={})
        db.add(other)
        db.commit()
        db.add(Lead(client_id=other.id, phone="+19195550177", source="web_form", full_name="Secret Customer"))
        db.commit()
        return db.query(Lead).filter_by(full_name="Secret Customer").one().id


def test_password_hashing():
    h = hash_password(STAFF_PASSWORD)
    assert h.startswith("scrypt$") and STAFF_PASSWORD not in h
    assert verify_password(STAFF_PASSWORD, h) and not verify_password("wrong password!", h)
    assert hash_password(STAFF_PASSWORD) != h  # salted
    try:
        hash_password("short")
        raise AssertionError("short passwords must be rejected")
    except ValueError:
        pass


def test_login_then_inbox(env):
    env.add_user()
    r = env.login()
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "Leads" in env.http.get("/admin").text


def test_wrong_password_and_unknown_email(env):
    env.add_user()
    assert env.login(password="wrong password here").status_code == 401
    assert env.login(email="nobody@example.com").status_code == 401
    assert env.http.get("/admin", follow_redirects=False).status_code == 303  # still logged out


def test_login_throttled_after_repeated_failures(env):
    env.add_user()
    for _ in range(8):
        assert env.login(password="wrong password here").status_code == 401
    r = env.login()  # even the right password is refused while throttled
    assert r.status_code == 429 and "Too many failed attempts" in r.text


def test_owner_sees_only_their_business(env):
    other_lead = make_other_business(env)
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form()
    env.add_user()
    env.login()
    inbox = env.http.get("/admin").text
    assert "Jordan Lee" in inbox and "Secret Customer" not in inbox
    assert env.http.get(f"/admin/leads/{other_lead}").status_code == 404
    r = env.http.post(f"/admin/leads/{other_lead}/reply",
                      data={"csrf": env.csrf("/admin"), "body": "hi"})
    assert r.status_code == 404 and env.sms.sent[-1]["to"] != "+19195550177"


def test_operator_sees_every_business(env):
    make_other_business(env)
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form()
    env.add_user(email="me@example.com", client_slug=None)
    env.login(email="me@example.com")
    inbox = env.http.get("/admin").text
    assert "Jordan Lee" in inbox and "Secret Customer" in inbox and "Other HVAC Co" in inbox


def test_csrf_required_on_every_form(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form()
    env.add_user()
    env.login()
    sent = len(env.sms.sent)
    assert env.http.post("/admin/leads/1/reply", data={"body": "hi"}).status_code == 403
    assert env.http.post("/admin/leads/1/reply", data={"csrf": "forged", "body": "hi"}).status_code == 403
    assert env.http.post("/admin/leads/1/pause", data={}).status_code == 403
    assert len(env.sms.sent) == sent and not env.lead().paused
    # Login itself also needs the token (stops login CSRF).
    fresh = TestClient(env.app, base_url="https://testserver")
    r = fresh.post("/admin/login", data={"email": "owner@example.com", "password": STAFF_PASSWORD})
    assert r.status_code == 403


def test_logout_and_password_change_end_sessions(env):
    env.add_user()
    env.login()
    env.http.post("/admin/logout", data={"csrf": env.csrf("/admin")})
    assert env.http.get("/admin", follow_redirects=False).status_code == 303

    env.login()
    assert env.http.get("/admin").status_code == 200
    with env.sf() as db:  # password reset (e.g. via scripts/create_user.py)
        u = db.query(StaffUser).one()
        u.password_hash = hash_password("a brand new password")
        db.commit()
    assert env.http.get("/admin", follow_redirects=False).status_code == 303


def test_deactivated_user_is_logged_out(env):
    env.add_user()
    env.login()
    with env.sf() as db:
        db.query(StaffUser).one().active = False
        db.commit()
    assert env.http.get("/admin", follow_redirects=False).status_code == 303
    assert env.login().status_code == 401


def test_redirect_after_login_is_local_only():
    assert safe_next("/admin/leads/5") == "/admin/leads/5"
    for bad in ["//evil.com", "https://evil.com", "/demo", "/admin\\@evil.com", None, ""]:
        assert safe_next(bad) == "/admin"


def test_login_returns_to_the_alert_link(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form()
    env.add_user()
    r = env.http.get("/admin/leads/1", follow_redirects=False)
    assert r.headers["location"] == "/admin/login?next=/admin/leads/1"
    page = env.http.get(r.headers["location"]).text
    r = env.http.post("/admin/login", data={"email": "owner@example.com", "password": STAFF_PASSWORD,
                                            "csrf": csrf_from(page), "next": "/admin/leads/1"},
                      follow_redirects=False)
    assert r.headers["location"] == "/admin/leads/1"


def test_alert_links_carry_no_secrets(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form(sms_consent=False)
    alert = env.alerts.sent[-1]["text"]
    assert "https://agent.example.com/admin/leads/1" in alert and "token" not in alert.lower()


def test_staff_actions_are_audited(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.submit_form()
    env.add_user()
    env.login()
    env.http.post("/admin/leads/1/reply", data={"csrf": env.csrf("/admin/leads/1"), "body": "On it!"})
    from app.models import EventLog
    with env.sf() as db:
        types = [e.type for e in db.query(EventLog)]
    assert "staff_login" in types and "staff_reply" in types


def test_security_headers_on_staff_pages(env):
    r = env.http.get("/admin/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
