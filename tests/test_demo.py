"""The /demo page and its API, driven by the scripted LLM (no keys needed)."""
from conftest import text, tool


def demo(env):
    return env.http.app.state.demo


def state(env):
    demo(env).session.wait_idle()
    return env.http.get("/demo/api/state").json()


def test_page_serves_html(env):
    r = env.http.get("/demo")
    assert r.status_code == 200 and "Business owner live view" in r.text and "cdn.tailwindcss.com" in r.text


def test_empty_state(env):
    s = state(env)
    assert s["messages"] == [] and s["lead"] is None and s["bookings"] == [] and s["llm_ready"]
    assert s["business"] == "Triangle Comfort Heating & Air"


def test_form_to_booking_shows_up_live(env):
    opener = "Hi Jordan, this is Ava at Triangle Comfort Heating & Air. What ZIP is the home in? Reply STOP to opt out."
    env.llm.script(text(opener))
    r = env.http.post("/demo/api/form", json={"name": "Jordan Lee", "zip": "", "issue": "AC blowing warm air"})
    assert r.status_code == 200
    s = state(env)
    assert s["messages"][0]["role"] == "event" and s["messages"][0]["kind"] == "form"
    assert s["messages"][1] == {**s["messages"][1], "role": "agent", "body": opener, "chars": len(opener)}
    assert s["lead"]["status"] == "Active" and s["lead"]["name"] == "Jordan Lee"
    assert env.sms.sent == []  # the real app's phone is never touched by the demo

    env.llm.script([tool("check_service_area", zip_code="27513")],
                   [tool("get_available_slots", urgency="this_week")],
                   text("I have a few openings this week. Which works?"))
    env.http.post("/demo/api/sms", json={"body": "27513, this week"})
    s = state(env)
    assert s["lead"]["in_area"] is True

    from app.models import SlotOffer
    with demo(env).session.sf() as db:
        slot = db.query(SlotOffer).order_by(SlotOffer.created_at).first()
        slot_id, label = slot.id, slot.label
    env.llm.script([tool("book_appointment", slot_id=slot_id, full_name="Jordan Lee", notes="AC warm air")],
                   text(f"You're booked for {label}!"))
    env.http.post("/demo/api/sms", json={"body": "the first one"})
    s = state(env)
    assert s["lead"]["status"] == "Booked"
    assert len(s["bookings"]) == 1 and s["bookings"][0]["label"] == label
    assert s["alerts"][0]["title"].startswith("New booking")
    assert "http" not in s["alerts"][0]["text"]  # admin links are stripped


def test_prefilled_full_window_is_not_offered(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "27513", "issue": "No heat"})
    state(env)
    env.llm.script([tool("check_service_area", zip_code="27513")],
                   [tool("get_available_slots", urgency="within_48_hours")], text("Here are the times."))
    env.http.post("/demo/api/sms", json={"body": "tomorrow"})
    state(env)
    from app.models import SlotOffer
    session = demo(env).session
    full_start = session.calendar.events[0]["start"]
    with session.sf() as db:
        starts = [o.start for o in db.query(SlotOffer)]
    assert starts and full_start not in starts  # the full 8-10am window is never offered


def test_stop_shows_opted_out(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"})
    state(env)
    env.http.post("/demo/api/sms", json={"body": "STOP"})
    s = state(env)
    assert s["lead"]["status"] == "Opted Out"
    assert s["messages"][-1]["role"] == "system_notice"  # the unsubscribe confirmation


def test_escalation_and_urgent_alert(env):
    env.llm.script(text("Hi, sorry we missed your call! Reply STOP to opt out."))
    env.http.post("/demo/api/missed-call")
    assert state(env)["messages"][0]["kind"] == "missed_call"
    env.llm.script([tool("escalate_to_human", reason="emergency", urgency="urgent", summary="Gas smell")],
                   text("Please get everyone out now and call 911 and your gas company."))
    env.http.post("/demo/api/sms", json={"body": "I smell gas"})
    s = state(env)
    assert s["lead"]["status"] == "Escalated"
    assert s["alerts"][0]["urgent"] and "http" not in s["alerts"][0]["text"]
    env.http.post("/demo/api/sms", json={"body": "hello?"})
    s = state(env)
    assert s["messages"][-1]["body"] == "hello?"  # no automated reply while escalated


def test_reset_clears_everything(env):
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"})
    assert state(env)["messages"]
    env.http.post("/demo/api/reset")
    s = state(env)
    assert s["messages"] == [] and s["lead"] is None and s["alerts"] == []


def test_demo_token_and_disable(env):
    env.settings.demo_token = "secret"
    assert env.http.get("/demo").status_code == 403
    assert env.http.get("/demo?token=secret").status_code == 200
    assert env.http.get("/demo/api/state?token=secret").status_code == 200
    env.settings.enable_demo = False
    assert env.http.get("/demo?token=secret").status_code == 404


def test_missing_llm_key_is_reported(env):
    from app.demo import DemoManager

    def broken():
        raise RuntimeError("Set GEMINI_API_KEY in your .env file, then restart the server.")

    env.http.app.state.demo.llm_factory = broken
    env.http.post("/demo/api/reset")
    s = state(env)
    assert s["llm_ready"] is False and "GEMINI_API_KEY" in s["llm_error"]
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"})
    assert "GEMINI_API_KEY" in state(env)["errors"][-1]


# ---------- Public-link safety: private sessions and usage caps ----------

def test_visitors_get_private_conversations(env):
    a, b = {"X-Demo-Session": "visitor-aaaa-1111"}, {"X-Demo-Session": "visitor-bbbb-2222"}
    env.llm.script(text("Hi Alice! Reply STOP to opt out."))
    env.http.post("/demo/api/form", json={"name": "Alice", "zip": "", "issue": "No heat"}, headers=a)
    demo(env).get("visitor-aaaa-1111").wait_idle()
    sa = env.http.get("/demo/api/state", headers=a).json()
    sb = env.http.get("/demo/api/state", headers=b).json()
    assert sa["lead"]["name"] == "Alice" and len(sa["messages"]) == 2
    assert sb["lead"] is None and sb["messages"] == []
    env.http.post("/demo/api/reset", headers=b)  # resetting B leaves A alone
    assert env.http.get("/demo/api/state", headers=a).json()["lead"]["name"] == "Alice"


def test_bad_session_id_rejected(env):
    assert env.http.get("/demo/api/state", headers={"X-Demo-Session": "<script>"}).status_code == 400


def test_per_session_message_cap(env):
    env.settings.demo_max_actions_per_session = 2
    env.llm.script(text("Hi! Reply STOP to opt out."), text("What ZIP?"))
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"})
    state(env)
    env.http.post("/demo/api/sms", json={"body": "hi"})
    state(env)
    r = env.http.post("/demo/api/sms", json={"body": "again"})
    assert r.status_code == 429 and "Reset Demo" in r.json()["detail"]
    env.http.post("/demo/api/reset")
    env.llm.script(text("Hi again! Reply STOP to opt out."))
    assert env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"}).status_code == 200


def test_daily_cap_across_visitors(env):
    env.settings.demo_daily_action_limit = 1
    env.llm.script(text("Hi! Reply STOP to opt out."))
    env.http.post("/demo/api/form", json={"name": "Jo", "zip": "", "issue": "x"})
    r = env.http.post("/demo/api/missed-call", headers={"X-Demo-Session": "another-visitor-1"})
    assert r.status_code == 429 and "today's usage limit" in r.json()["detail"]


def test_root_redirects_to_demo_keeping_token(env):
    r = env.http.get("/?token=abc", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/demo?token=abc"
