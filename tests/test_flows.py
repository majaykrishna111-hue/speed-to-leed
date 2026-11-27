"""End-to-end tests through the real webhooks, agent loop and tool handlers."""
from datetime import timedelta

from conftest import ADMIN, BUSINESS_NUMBER, LEAD_PHONE, last_tool_result, text, tool
from twilio.request_validator import RequestValidator

OPENER = "Hi Jordan, this is Ava at Triangle Comfort Heating & Air. Sorry your AC is blowing warm air! What ZIP is the home in? Reply STOP to opt out."


def first_offered_slot(env) -> str:
    from app.models import SlotOffer
    with env.sf() as db:
        return db.query(SlotOffer).order_by(SlotOffer.created_at).first().id


# ---------- Scenario 1: happy path booking ----------

def test_happy_path_books_real_calendar_slot(env):
    env.llm.script([tool("save_lead_details", service_type="repair", issue_summary="AC blowing warm air"),
                    ], text(OPENER))
    r = env.submit_form()
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert env.sms.sent[-1]["body"] == OPENER
    assert env.sms.sent[-1]["to"] == LEAD_PHONE and env.sms.sent[-1]["from"] == BUSINESS_NUMBER

    env.llm.script([tool("check_service_area", zip_code="27513")],
                   text("Thanks! How soon do you need someone out?"))
    env.lead_texts("27513")
    assert env.lead().in_service_area is True

    env.llm.script([tool("save_lead_details", urgency="within_48_hours")],
                   [tool("get_available_slots", urgency="within_48_hours")],
                   text("I have Sat Jul 11, 8-10am or Sat Jul 11, 10am-12pm. Which works?"))
    env.lead_texts("asap, in the next day or two")
    # The offered slots are shown to the model on the next turn, so it can book them later.
    system_dynamic = env.llm.calls[-1]["system"][1]["text"]
    assert "Slots already offered" not in system_dynamic  # not yet: offered during this run

    slot_id = first_offered_slot(env)
    env.llm.script([tool("book_appointment", slot_id=slot_id, full_name="Jordan Lee", email="jordan@example.com",
                         notes="AC blowing warm air, needs repair within 48 hours")],
                   text("You're booked for Sat Jul 11, 8-10am. Your technician will text 30 minutes before arriving."))
    env.lead_texts("the 8am one. Jordan Lee, jordan@example.com")
    assert f"slot_id {slot_id}: Sat Jul 11, 8-10am" in env.llm.calls[-2]["system"][1]["text"]

    lead = env.lead()
    assert lead.status == "booked" and lead.appointment_event_id
    booked = [e for e in env.cal.events]
    assert len(booked) == 1
    assert LEAD_PHONE in booked[0]["description"]  # phone comes from the server
    from conftest import TZ
    assert booked[0]["start"].astimezone(TZ).hour == 8  # 8am Eastern, stored as UTC
    assert any(a["title"].startswith("New booking") for a in env.alerts.sent)


def test_cannot_book_invented_slot(env):
    env.llm.script(text(OPENER))
    env.submit_form(zip="27513")
    env.llm.script([tool("check_service_area", zip_code="27513")],
                   [tool("book_appointment", slot_id="s_abcdef", full_name="Jordan Lee", notes="x")],
                   lambda m: (_ for _ in ()).throw(AssertionError("unreachable")) if "error" not in last_tool_result(m)
                   else text("Let me grab current openings. Morning or afternoon?"))
    env.lead_texts("book me for tomorrow at 9")
    assert env.cal.events == []
    assert env.lead().status == "active"


def test_slots_blocked_until_service_area_checked(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("get_available_slots", urgency="flexible")],
                   lambda m: text("What ZIP code is the home in?") if "error" in last_tool_result(m)
                   else text("WRONG"))
    env.lead_texts("whenever works")
    assert env.sms.sent[-1]["body"] == "What ZIP code is the home in?"


def test_full_windows_not_offered(env):
    from datetime import datetime
    from conftest import TZ
    # Two jobs (capacity 2) already fill Saturday 8-10.
    for _ in range(2):
        s = datetime(2026, 7, 11, 8, tzinfo=TZ)
        env.cal.create_event("cal", s, s + timedelta(hours=2), "Existing", "", "America/New_York")
    env.llm.script(text(OPENER))
    env.submit_form(zip="27513")
    env.llm.script([tool("check_service_area", zip_code="27513")],
                   [tool("get_available_slots", urgency="this_week")],
                   lambda m: text("ok " + str(len(last_tool_result(m)["slots"]))))
    env.lead_texts("this week")
    with env.sf() as db:
        from app.models import SlotOffer
        labels = [o.label for o in db.query(SlotOffer)]
    assert "Sat Jul 11, 8-10am" not in labels
    assert labels[0] == "Sat Jul 11, 10am-12pm"


# ---------- Scenario 2: out of area ----------

def test_out_of_area_closes_without_booking(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("check_service_area", zip_code="28202")],
                   lambda m: [tool("close_conversation", reason="out_of_area")] if not last_tool_result(m)["in_area"] else text("WRONG"),
                   text("Sorry, we don't serve that area. Hope you get it sorted soon!"))
    env.lead_texts("28202")
    lead = env.lead()
    assert lead.in_service_area is False and lead.status == "closed" and lead.closed_reason == "out_of_area"
    assert env.cal.events == []


# ---------- Scenario 3/4: escalation and human takeover ----------

def test_escalation_alerts_owner_and_pauses(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("escalate_to_human", reason="requested_human", urgency="normal",
                         summary="Wants to talk to the owner about a warranty claim")],
                   lambda m: text(f"Got it. A team member will reach out {last_tool_result(m)['team_will_respond']}."))
    env.lead_texts("can I talk to a real person about my warranty")
    lead = env.lead()
    assert lead.paused and lead.status == "escalated"
    assert any("needs a person" in a["title"] for a in env.alerts.sent)
    assert "office is open now" in env.sms.sent[-1]["body"]

    sent_before, calls_before = len(env.sms.sent), len(env.llm.calls)
    env.lead_texts("hello?")
    assert len(env.sms.sent) == sent_before and len(env.llm.calls) == calls_before  # agent stays quiet
    assert env.alerts.sent[-1]["title"] == "New text from a lead you're handling"


def test_emergency_alert_is_urgent(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("escalate_to_human", reason="emergency", urgency="urgent", summary="Gas smell")],
                   text("Please get everyone out of the house now and call 911 and your gas company. We've alerted our team."))
    env.lead_texts("I smell gas near the furnace")
    assert env.alerts.sent[-1]["urgent"] is True
    assert env.lead().paused


def test_staff_reply_from_inbox_pauses_agent(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    r = env.http.post(f"/admin/leads/1/reply", data={"token": ADMIN, "body": "Hi Jordan, this is Mike the owner."},
                      follow_redirects=False)
    assert r.status_code == 303
    assert env.sms.sent[-1]["body"].startswith("Hi Jordan, this is Mike")
    calls = len(env.llm.calls)
    env.lead_texts("thanks Mike")
    assert len(env.llm.calls) == calls and env.lead().paused

    # Owner hands it back: the agent answers the next text, and sees the staff message as context.
    env.http.post("/admin/leads/1/resume", data={"token": ADMIN})
    env.llm.script(text("Happy to help get you scheduled. What ZIP is the home in?"))
    env.lead_texts("ok can I book now")
    history = env.llm.calls[-1]["messages"]
    assert any("(Sent by a team member)" in str(m["content"]) for m in history)


def test_admin_requires_token(env):
    assert env.http.get("/admin?token=wrong").status_code == 403


# ---------- Opt-out ----------

def test_stop_keyword_blocks_everything(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    calls = len(env.llm.calls)
    env.lead_texts("STOP")
    lead = env.lead()
    assert lead.opted_out and len(env.llm.calls) == calls
    sent = len(env.sms.sent)
    env.lead_texts("actually what are your prices")
    env.http.post("/webhooks/twilio/voice", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER})
    assert len(env.sms.sent) == sent  # no texts after STOP, even after a missed call
    assert env.alerts.sent[-1]["title"].startswith("Missed call from a number that opted out")


def test_informal_opt_out_confirms_once(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("close_conversation", reason="opt_out_request")], text("NO_REPLY"))
    env.lead_texts("please stop texting me")
    lead = env.lead()
    assert lead.opted_out and lead.closed_reason == "opt_out_request"
    assert env.sms.sent[-1]["body"].startswith("You're unsubscribed")


def test_carrier_level_opt_out_is_recorded(env):
    env.sms.opted_out_numbers.add(LEAD_PHONE)
    env.llm.script(text(OPENER))
    env.submit_form()
    assert env.lead().opted_out and env.sms.sent == []


def test_form_without_consent_does_not_text(env):
    r = env.submit_form(sms_consent=False)
    assert r.json()["status"] == "no_consent"
    assert env.sms.sent == [] and env.llm.calls == []
    assert "no texting consent" in env.alerts.sent[-1]["title"]


# ---------- Message guardrails ----------

def test_long_reply_is_rewritten(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script(text("x " * 200), text("Short and sweet. What ZIP is the home in?"))
    env.lead_texts("hi")
    assert env.sms.sent[-1]["body"] == "Short and sweet. What ZIP is the home in?"
    assert "SYSTEM NOTE" in str(env.llm.calls[-1]["messages"][-1]["content"])


def test_reply_still_too_long_falls_back_to_human(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script(text("y " * 200), text("z " * 200))
    env.lead_texts("hi")
    assert env.sms.sent[-1]["body"].startswith("Thanks for your message! A team member")
    assert env.lead().paused
    assert all(len(m["body"]) <= 300 for m in env.sms.sent)


def test_leaked_slot_id_is_rewritten(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script(text("Booked slot s_1a2b3c for you!"), text("You're all set!"))
    env.lead_texts("ok")
    assert env.sms.sent[-1]["body"] == "You're all set!"


def test_smart_punctuation_converted_to_ascii(env):
    env.llm.script(text("Hi Jordan — we’re on it… Reply STOP to opt out."))
    env.submit_form()
    assert env.sms.sent[-1]["body"] == "Hi Jordan - we're on it... Reply STOP to opt out."


def test_opener_falls_back_to_template_when_claude_fails(env):
    env.llm.script(TimeoutError("slow"))
    env.submit_form()
    body = env.sms.sent[-1]["body"]
    assert body.startswith("Hi Jordan, this is Ava with Triangle Comfort") and body.endswith("Reply STOP to opt out.")
    assert not env.lead().paused  # conversation continues normally


def test_no_reply_after_thanks(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    sent = len(env.sms.sent)
    env.llm.script(text("NO_REPLY"))
    env.lead_texts("thanks!")
    assert len(env.sms.sent) == sent


def test_daily_cap_hands_to_human(env):
    env.settings.max_agent_texts_per_day = 2
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script(text("What ZIP is the home in?"))
    env.lead_texts("hi")
    calls = len(env.llm.calls)
    env.lead_texts("hi again")
    assert len(env.llm.calls) == calls and env.lead().paused
    assert env.alerts.sent[-1]["title"].startswith("Please take over")


def test_invalid_tool_input_is_reported_to_model(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    env.llm.script([tool("check_service_area", zip_code="2751")],
                   lambda m: text("Could you double-check that ZIP code?") if "error" in last_tool_result(m) else text("WRONG"))
    env.lead_texts("2751")
    assert env.sms.sent[-1]["body"] == "Could you double-check that ZIP code?"


# ---------- Missed calls ----------

def test_missed_call_forward_first_mode(env):
    with env.sf() as db:
        from app.models import Client
        c = db.query(Client).first()
        c.config = {**c.config, "call_mode": "forward_first", "forward_to_number": "+19195550111"}
        db.commit()
    r = env.http.post("/webhooks/twilio/voice", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER})
    assert "<Dial" in r.text and "+19195550111" in r.text and env.sms.sent == []

    env.http.post("/webhooks/twilio/voice-status", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER,
                                                         "DialCallStatus": "completed"})
    assert env.sms.sent == []  # answered: no text

    env.llm.script(text("Hi, this is Ava with Triangle Comfort Heating & Air. Sorry we missed your call! What's going on with your heating or cooling? Reply STOP to opt out."))
    r = env.http.post("/webhooks/twilio/voice-status", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER,
                                                             "DialCallStatus": "no-answer"})
    assert "<Say>" in r.text
    assert env.sms.sent[-1]["body"].startswith("Hi, this is Ava")
    assert "nobody answered" in str(env.llm.calls[-1]["messages"][0]["content"])


def test_second_missed_call_continues_existing_conversation(env):
    env.llm.script(text("Hi, sorry we missed your call! How can we help? Reply STOP to opt out."))
    env.http.post("/webhooks/twilio/voice", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER})
    env.clock["now"] += timedelta(minutes=10)
    env.llm.script(text("Saw you called again - happy to help by text. What's going on?"))
    env.http.post("/webhooks/twilio/voice", data={"From": LEAD_PHONE, "To": BUSINESS_NUMBER})
    last_user = env.llm.calls[-1]["messages"][-1]["content"]
    assert "[EVENT] They just called" in last_user
    assert "[NEW_LEAD]" not in last_user


# ---------- Security ----------

def test_twilio_signature_required(env):
    env.settings.validate_twilio_signature = True
    params = {"From": LEAD_PHONE, "To": BUSINESS_NUMBER, "Body": "hi", "MessageSid": "SM1"}
    r = env.http.post("/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": "bad"})
    assert r.status_code == 403
    sig = RequestValidator("tw-secret").compute_signature("https://agent.example.com/webhooks/twilio/sms", params)
    env.llm.script(text("Hi! This is Ava with Triangle Comfort. How can we help?"))
    r = env.http.post("/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and env.sms.sent


def test_form_requires_secret(env):
    r = env.http.post("/webhooks/form/triangle-comfort", json={"phone": "9195550142"},
                      headers={"X-Form-Key": "nope"})
    assert r.status_code == 403


def test_prompt_contains_business_config_and_no_placeholders(env):
    env.llm.script(text(OPENER))
    env.submit_form()
    static, dynamic = env.llm.calls[0]["system"]
    assert "{{" not in static["text"] + dynamic["text"]
    assert "Triangle Comfort Heating & Air" in static["text"] and "$89" in static["text"]
    assert static.get("cache_control") and "Friday, July 10, 2026" in dynamic["text"]
    assert 'Form message (customer wrote): "AC running but blowing warm air"' in dynamic["text"]
