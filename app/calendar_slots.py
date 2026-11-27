"""Appointment slots: generating open windows from the business's schedule and live calendar,
plus the Google Calendar adapter and an in-memory fake for tests and the simulator."""
import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HORIZON_DAYS = {"emergency_today": 1, "within_48_hours": 2, "this_week": 7}


@dataclass
class Slot:
    start: datetime
    end: datetime
    label: str


class CalendarProvider(Protocol):
    def busy_intervals(self, calendar_id: str, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...
    def create_event(self, calendar_id: str, start: datetime, end: datetime,
                     summary: str, description: str, tz: str) -> str: ...


def _hour_label(t: datetime, with_suffix: bool) -> str:
    h = t.hour % 12 or 12
    text = f"{h}" if t.minute == 0 else f"{h}:{t.minute:02d}"
    return text + (("am" if t.hour < 12 else "pm") if with_suffix else "")


def slot_label(start: datetime, end: datetime) -> str:
    """'Tue Dec 2, 8-10am' or 'Tue Dec 2, 11am-1pm'. ASCII only, to keep SMS in the cheap GSM-7 encoding."""
    same_half = (start.hour < 12) == (end.hour < 12)
    rng = f"{_hour_label(start, not same_half)}-{_hour_label(end, True)}"
    return f"{start:%a %b} {start.day}, {rng}"


def compute_slots(cfg: dict, tz_name: str, now: datetime,
                  busy: list[tuple[datetime, datetime]], urgency: str,
                  earliest_date: Optional[date] = None, time_of_day: str = "any",
                  limit: int = 3, max_per_day: int = 2) -> list[Slot]:
    """Open slots from the client's weekly windows minus calendar events.

    cfg keys: slot_windows {"mon": [["08:00","10:00"], ...]}, slot_capacity (jobs per window,
    usually number of technicians), min_notice_minutes, horizon_days.
    """
    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    capacity = int(cfg.get("slot_capacity", 1))
    earliest_start = now + timedelta(minutes=int(cfg.get("min_notice_minutes", 120)))
    horizon = HORIZON_DAYS.get(urgency, int(cfg.get("horizon_days", 14)))
    windows = cfg.get("slot_windows", {})

    slots: list[Slot] = []
    for offset in range(horizon):
        day = local_now.date() + timedelta(days=offset)
        if earliest_date and day < earliest_date:
            continue
        per_day = 0
        for start_s, end_s in windows.get(DAY_KEYS[day.weekday()], []):
            start = datetime.combine(day, time.fromisoformat(start_s), tz)
            end = datetime.combine(day, time.fromisoformat(end_s), tz)
            if start < earliest_start:
                continue
            if time_of_day == "morning" and start.hour >= 12:
                continue
            if time_of_day == "afternoon" and start.hour < 12:
                continue
            overlapping = sum(1 for b_start, b_end in busy if b_start < end and start < b_end)
            if overlapping >= capacity:
                continue
            slots.append(Slot(start, end, slot_label(start, end)))
            per_day += 1
            if len(slots) >= limit:
                return slots
            if per_day >= max_per_day:
                break
    return slots


class GoogleCalendar:
    """Reads and writes the client's Google Calendar through a service account.
    The client shares their calendar with the service account's email ("Make changes to events")."""

    def __init__(self, service_account_json_b64: str = "", service=None):
        if service is not None:  # tests inject a service backed by fake HTTP
            self.service = service
            self.service_account_email = "test@example.iam.gserviceaccount.com"
            return
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        try:
            info = json.loads(base64.b64decode(service_account_json_b64))
        except (ValueError, TypeError) as exc:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid base64-encoded JSON") from exc
        self.service_account_email = info.get("client_email", "")
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"])
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Used by scripts/preflight.py to clean up its test event."""
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def busy_intervals(self, calendar_id, start, end):
        items, page_token = [], None
        while True:
            resp = self.service.events().list(
                calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=2500, pageToken=page_token,
            ).execute()
            items += resp.get("items", [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        busy = []
        for ev in items:
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            s, e = ev["start"], ev["end"]
            if "dateTime" in s:
                busy.append((datetime.fromisoformat(s["dateTime"]), datetime.fromisoformat(e["dateTime"])))
            else:  # all-day event (holiday, "office closed"): block those whole days in local time
                local_tz = start.tzinfo or timezone.utc
                busy.append((datetime.combine(date.fromisoformat(s["date"]), time(0), local_tz),
                             datetime.combine(date.fromisoformat(e["date"]), time(0), local_tz)))
        return busy

    def create_event(self, calendar_id, start, end, summary, description, tz):
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
        return self.service.events().insert(calendarId=calendar_id, body=body).execute()["id"]


@dataclass
class FakeCalendar:
    """In-memory calendar for tests and the terminal simulator."""
    events: list[dict] = field(default_factory=list)

    def busy_intervals(self, calendar_id, start, end):
        return [(e["start"], e["end"]) for e in self.events if e["start"] < end and start < e["end"]]

    def create_event(self, calendar_id, start, end, summary, description, tz):
        event_id = f"evt_{len(self.events) + 1}"
        self.events.append({"id": event_id, "start": start, "end": end,
                            "summary": summary, "description": description})
        return event_id
