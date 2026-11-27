"""The Gemini adapter, tested offline with a stand-in for the google-genai client that returns
real google.genai response objects. Proves the same agent loop, tools and guardrails work."""
import json
from types import SimpleNamespace

from google.genai import errors, types

from app.adapters import GeminiLLM, gemini_schema
from app.prompt import load_tools
from conftest import LEAD_PHONE


def fc_part(name, args, call_id=None, signature=None):
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args),
                      thought_signature=signature)


def reply(*parts):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class FakeGenai:
    def __init__(self):
        self.queue, self.requests = [], []
        self.models = SimpleNamespace(generate_content=self._generate)

    def script(self, *items):
        self.queue.extend(items)

    def _generate(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gemini(fake=None, budget=0):
    fake = fake or FakeGenai()
    return GeminiLLM("key", "gemini-2.5-flash", budget, client=fake), fake


def test_schema_conversion_keeps_meaning_and_drops_unsupported_keys():
    tools = load_tools()
    book = next(t for t in tools if t["name"] == "get_available_slots")
    out = gemini_schema(book["input_schema"])
    assert out["required"] == ["urgency"] and out["additionalProperties"] is False
    assert "format" not in out["properties"]["earliest_date"]
    assert "YYYY-MM-DD" in out["properties"]["earliest_date"]["description"]
    zip_schema = gemini_schema(next(t for t in tools if t["name"] == "check_service_area")["input_schema"])
    assert "pattern" not in zip_schema["properties"]["zip_code"]
    assert "Exactly 5 digits." in zip_schema["properties"]["zip_code"]["description"]
    assert all("cache_control" not in json.dumps(gemini_schema(t["input_schema"])) for t in tools)


def test_request_uses_same_prompt_and_tools_with_no_auto_calling():
    llm, fake = gemini()
    fake.script(reply(types.Part(text="Hi there")))
    system = [{"type": "text", "text": "STATIC", "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": "DYNAMIC"}]
    resp = llm.create(system, load_tools(), [{"role": "user", "content": "hello"}], timeout=7)
    cfg = fake.requests[0]["config"]
    assert cfg.system_instruction == "STATIC\n\nDYNAMIC"
    assert [d.name for d in cfg.tools[0].function_declarations] == [t["name"] for t in load_tools()]
    assert cfg.automatic_function_calling.disable is True
    assert cfg.thinking_config.thinking_budget == 0
    assert cfg.http_options.timeout == 11000  # Gemini refuses deadlines under 10 s
    assert fake.requests[0]["model"] == "gemini-2.5-flash"
    assert resp.stop_reason == "end_turn" and resp.content[0].text == "Hi there"


def test_tool_round_trip_preserves_thought_signature_and_names():
    llm, fake = gemini()
    fake.script(reply(fc_part("check_service_area", {"zip_code": "27513"}, signature=b"sig-1")),
                reply(types.Part(text="You're in our area!")))
    first = llm.create([{"type": "text", "text": "S"}], load_tools(),
                       [{"role": "user", "content": "27513"}], timeout=10)
    assert first.stop_reason == "tool_use"
    call = first.content[0]
    assert call.name == "check_service_area" and call.input == {"zip_code": "27513"}

    messages = [{"role": "user", "content": "27513"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": call.id,
                                                   "name": call.name, "input": call.input}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id,
                                              "content": json.dumps({"in_area": True}), "is_error": False}]}]
    second = llm.create([{"type": "text", "text": "S"}], load_tools(), messages, timeout=10)
    sent = fake.requests[1]["contents"]
    assert [c.role for c in sent] == ["user", "model", "user"]
    assert sent[1].parts[0].thought_signature == b"sig-1"  # original part sent back unchanged
    fr = sent[2].parts[0].function_response
    assert fr.name == "check_service_area" and fr.response == {"in_area": True}
    assert second.content[0].text == "You're in our area!"


def test_thought_parts_are_never_sent_as_sms_text():
    llm, fake = gemini()
    fake.script(reply(types.Part(text="Let me think about pricing...", thought=True),
                      types.Part(text="What ZIP is the home in?")))
    resp = llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 7)
    assert [b.text for b in resp.content] == ["What ZIP is the home in?"]


def test_rate_limit_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.adapters.time.sleep", lambda s: None)
    llm, fake = gemini()
    fake.script(errors.APIError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
                reply(types.Part(text="ok")))
    resp = llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 20)
    assert resp.content[0].text == "ok" and len(fake.requests) == 2


def test_blocked_response_becomes_empty_reply():
    llm, fake = gemini()
    fake.script(types.GenerateContentResponse(candidates=[]))
    resp = llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 7)
    assert resp.content == [] and resp.stop_reason == "end_turn"


# ---------- The real agent loop running on the Gemini adapter ----------

def test_full_booking_through_gemini_adapter(env):
    llm, fake = gemini()
    env.deps.llm = llm
    opener = "Hi Jordan, this is Ava at Triangle Comfort Heating & Air. What ZIP is the home in? Reply STOP to opt out."
    fake.script(reply(types.Part(text=opener)))
    env.submit_form()
    assert env.sms.sent[-1]["body"] == opener

    fake.script(reply(fc_part("check_service_area", {"zip_code": "27513"}),
                      fc_part("save_lead_details", {"urgency": "within_48_hours"})),
                reply(fc_part("get_available_slots", {"urgency": "within_48_hours"})),
                reply(types.Part(text="I have Sat Jul 11, 8-10am or 10am-12pm. Which works?")))
    env.lead_texts("27513, tomorrow if possible")
    assert env.lead().in_service_area is True and env.lead().urgency == "within_48_hours"

    from app.models import SlotOffer
    with env.sf() as db:
        slot_id = db.query(SlotOffer).order_by(SlotOffer.created_at).first().id
    fake.script(reply(fc_part("book_appointment", {"slot_id": slot_id, "full_name": "Jordan Lee",
                                                   "notes": "AC blowing warm air"})),
                reply(types.Part(text="You're booked for Sat Jul 11, 8-10am!")))
    env.lead_texts("8-10 works")
    assert env.lead().status == "booked" and LEAD_PHONE in env.cal.events[0]["description"]
    assert env.sms.sent[-1]["body"] == "You're booked for Sat Jul 11, 8-10am!"


def test_gemini_long_reply_still_hits_300_char_guardrail(env):
    llm, fake = gemini()
    env.deps.llm = llm
    fake.script(reply(types.Part(text="Hi! Reply STOP to opt out.")))
    env.submit_form()
    fake.script(reply(types.Part(text="word " * 100)), reply(types.Part(text="Short version. What ZIP?")))
    env.lead_texts("hi")
    assert env.sms.sent[-1]["body"] == "Short version. What ZIP?"
    last_request_text = fake.requests[-1]["contents"][-1].parts[0].text
    assert "SYSTEM NOTE" in last_request_text and "characters" in last_request_text


def test_gemini_invented_slot_is_rejected(env):
    llm, fake = gemini()
    env.deps.llm = llm
    fake.script(reply(types.Part(text="Hi! Reply STOP to opt out.")))
    env.submit_form(zip="27513")
    fake.script(reply(fc_part("check_service_area", {"zip_code": "27513"})),
                reply(fc_part("book_appointment", {"slot_id": "s_000000", "full_name": "J", "notes": "x"})),
                reply(types.Part(text="Let me check what's open. Morning or afternoon?")))
    env.lead_texts("book me tomorrow 9am")
    sent_back = fake.requests[-1]["contents"][-1].parts[0].function_response.response
    assert "error" in sent_back and env.cal.events == []


def test_gemini_3_uses_thinking_level_and_leaves_room_for_thoughts():
    fake = FakeGenai()
    llm = GeminiLLM("key", "gemini-3.8-flash", client=fake, thinking_level="low")
    fake.script(reply(types.Part(text="Hi")))
    llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 7)
    cfg = fake.requests[0]["config"]
    assert fake.requests[0]["model"] == "gemini-3.8-flash"
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert cfg.thinking_config.thinking_budget is None
    assert cfg.max_output_tokens == 600 + 2048


def test_our_short_deadline_is_enforced_client_side():
    import time as _time
    fake = FakeGenai()
    slow = SimpleNamespace(generate_content=lambda **kw: (_time.sleep(1.5), reply(types.Part(text="late")))[1])
    fake.models = slow
    llm = GeminiLLM("key", "gemini-3.8-flash", client=fake)
    started = _time.monotonic()
    try:
        llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], timeout=0.5)
        raise AssertionError("should have timed out")
    except TimeoutError:
        pass
    assert _time.monotonic() - started < 1.2


def test_retry_waits_for_googles_suggested_delay(monkeypatch):
    waits = []
    monkeypatch.setattr("app.adapters.time.sleep", lambda s: waits.append(s))
    llm, fake = gemini()
    err = errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                          "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                       "retryDelay": "6s"}]}})
    fake.script(err, reply(types.Part(text="ok")))
    resp = llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 25)
    assert waits == [6.0] and resp.content[0].text == "ok"


def test_retry_skipped_when_suggested_delay_does_not_fit(monkeypatch):
    monkeypatch.setattr("app.adapters.time.sleep", lambda s: None)
    llm, fake = gemini()
    err = errors.APIError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                          "details": [{"retryDelay": "40s"}]}})
    fake.script(err)
    try:
        llm.create([{"type": "text", "text": "S"}], load_tools(), [{"role": "user", "content": "hi"}], 25)
        raise AssertionError("expected the 429 to surface")
    except errors.APIError as exc:
        assert exc.code == 429 and len(fake.requests) == 1
