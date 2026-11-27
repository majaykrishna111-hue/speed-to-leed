from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.agent import problem_with, sanitize
from app.calendar_slots import compute_slots, slot_label
from app.tool_handlers import owner_response_time

TZ = ZoneInfo("America/New_York")
CFG = {"slot_windows": {"fri": [["08:00", "10:00"], ["15:00", "17:00"]],
                        "sat": [["08:00", "10:00"], ["10:00", "12:00"]],
                        "mon": [["08:00", "10:00"], ["13:00", "15:00"]]},
       "slot_capacity": 1, "min_notice_minutes": 120, "horizon_days": 14,
       "office_hours": {"mon": ["07:00", "18:00"], "fri": ["07:00", "18:00"], "sat": ["08:00", "12:00"]}}
FRI_2PM = datetime(2026, 7, 10, 14, 0, tzinfo=TZ)


def test_labels():
    s = datetime(2026, 7, 11, 8, tzinfo=TZ)
    assert slot_label(s, s + timedelta(hours=2)) == "Sat Jul 11, 8-10am"
    s = datetime(2026, 7, 11, 11, tzinfo=TZ)
    assert slot_label(s, s + timedelta(hours=2)) == "Sat Jul 11, 11am-1pm"
    s = datetime(2026, 7, 13, 13, tzinfo=TZ)
    assert slot_label(s, s + timedelta(hours=2)) == "Mon Jul 13, 1-3pm"


def test_min_notice_and_limit():
    slots = compute_slots(CFG, "America/New_York", FRI_2PM, [], "this_week")
    # Fri 3pm is inside the 2-hour notice window; max 2 per day; 3 total.
    assert [s.label for s in slots] == ["Sat Jul 11, 8-10am", "Sat Jul 11, 10am-12pm", "Mon Jul 13, 8-10am"]


def test_emergency_today_only():
    assert compute_slots(CFG, "America/New_York", FRI_2PM, [], "emergency_today") == []
    early = datetime(2026, 7, 10, 6, 0, tzinfo=TZ)
    slots = compute_slots(CFG, "America/New_York", early, [], "emergency_today")
    assert [s.label for s in slots] == ["Fri Jul 10, 8-10am", "Fri Jul 10, 3-5pm"]


def test_busy_and_preferences():
    busy = [(datetime(2026, 7, 11, 8, 30, tzinfo=TZ), datetime(2026, 7, 11, 9, 30, tzinfo=TZ))]
    slots = compute_slots(CFG, "America/New_York", FRI_2PM, busy, "flexible", time_of_day="afternoon")
    assert all(s.start.hour >= 12 for s in slots) and slots[0].label == "Mon Jul 13, 1-3pm"
    slots = compute_slots(CFG, "America/New_York", FRI_2PM, busy, "flexible")
    assert slots[0].label == "Sat Jul 11, 10am-12pm"  # 8-10 is taken
    slots = compute_slots(CFG, "America/New_York", FRI_2PM, [], "flexible", earliest_date=date(2026, 7, 13))
    assert slots[0].label == "Mon Jul 13, 8-10am"


def test_owner_response_time():
    assert "open now" in owner_response_time(CFG, "America/New_York", FRI_2PM)
    fri_night = datetime(2026, 7, 10, 21, 0, tzinfo=TZ)
    assert owner_response_time(CFG, "America/New_York", fri_night) == "when the office opens at 8am tomorrow"
    sat_night = datetime(2026, 7, 11, 21, 0, tzinfo=TZ)
    assert owner_response_time(CFG, "America/New_York", sat_night) == "when the office opens Monday at 7am"


def test_sanitize_and_problems():
    assert sanitize('"**Hi** there"') == "Hi there"
    assert problem_with("NO_REPLY") is None
    assert problem_with("ok NO_REPLY") is not None
    assert problem_with("a" * 301) is not None
    assert problem_with("I have Tue 8-10am. Which works?") is None
    assert problem_with("Use slot_id s_abc123") is not None
    assert problem_with("We bring all our tools and parts.") is None
