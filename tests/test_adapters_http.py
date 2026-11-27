"""The real Twilio, Google Calendar and Slack/email adapter code, exercised against fake HTTP.
Checks the exact requests we send and how we read the answers, without any credentials."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httplib2
import pytest
from googleapiclient.discovery import build
from twilio.http import HttpClient
from twilio.http.response import Response as TwilioResponse

from app.adapters import AlertSender, OptedOutError, TwilioSMS
from app.calendar_slots import GoogleCalendar

TZ = ZoneInfo("America/New_York")


# ---------------- Twilio ----------------

class FakeTwilioHttp(HttpClient):
    def __init__(self, status, body):
        import logging
        super().__init__(logging.getLogger("fake-twilio"), False)
        self.status, self.body, self.requests = status, body, []

    def request(self, method, uri, params=None, data=None, headers=None, auth=None, timeout=None,
                allow_redirects=False):
        self.requests.append({"method": method, "uri": uri, "data": data, "auth": auth})
        return TwilioResponse(self.status, json.dumps(self.body))


def test_twilio_send_builds_correct_request():
    http = FakeTwilioHttp(201, {"sid": "SM123", "status": "queued"})
    sms = TwilioSMS("AC" + "0" * 32, "token", http_client=http)
    assert sms.send("+19195550100", "+19195550142", "Hi there") == "SM123"
    req = http.requests[0]
    assert req["method"] == "POST" and req["uri"].endswith("/Accounts/AC" + "0" * 32 + "/Messages.json")
    assert req["data"]["From"] == "+19195550100" and req["data"]["To"] == "+19195550142"
    assert req["data"]["Body"] == "Hi there" and req["auth"] == ("AC" + "0" * 32, "token")


def test_twilio_opted_out_error_is_recognised():
    http = FakeTwilioHttp(400, {"code": 21610, "message": "Attempt to send to unsubscribed recipient",
                                "status": 400})
    sms = TwilioSMS("AC" + "0" * 32, "token", http_client=http)
    with pytest.raises(OptedOutError):
        sms.send("+19195550100", "+19195550142", "Hi")


def test_twilio_other_errors_surface():
    from twilio.base.exceptions import TwilioRestException
    http = FakeTwilioHttp(400, {"code": 21211, "message": "Invalid 'To' Phone Number", "status": 400})
    sms = TwilioSMS("AC" + "0" * 32, "token", http_client=http)
    with pytest.raises(TwilioRestException) as exc:
        sms.send("+19195550100", "+1555", "Hi")
    assert exc.value.code == 21211


# ---------------- Google Calendar ----------------

class FakeGoogleHttp:
    """Stands in for httplib2.Http under googleapiclient. Replies from a queue and records requests."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def request(self, uri, method="GET", body=None, headers=None, **kw):
        self.requests.append({"uri": uri, "method": method, "body": body})
        status, payload = self.replies.pop(0)
        return httplib2.Response({"status": str(status)}), json.dumps(payload).encode()


def calendar(replies):
    http = FakeGoogleHttp(replies)
    service = build("calendar", "v3", http=http, static_discovery=True)
    return GoogleCalendar(service=service), http


def test_busy_intervals_reads_events_pages_and_skips_free_or_cancelled():
    start = datetime(2026, 7, 10, 0, 0, tzinfo=TZ)
    page1 = {"items": [
        {"status": "confirmed", "start": {"dateTime": "2026-07-11T08:00:00-04:00"},
         "end": {"dateTime": "2026-07-11T10:00:00-04:00"}},
        {"status": "confirmed", "transparency": "transparent",  # marked "Free": doesn't block
         "start": {"dateTime": "2026-07-11T13:00:00-04:00"}, "end": {"dateTime": "2026-07-11T14:00:00-04:00"}},
    ], "nextPageToken": "p2"}
    page2 = {"items": [
        {"status": "cancelled", "start": {"dateTime": "2026-07-12T08:00:00-04:00"},
         "end": {"dateTime": "2026-07-12T10:00:00-04:00"}},
        {"status": "confirmed", "start": {"date": "2026-07-13"}, "end": {"date": "2026-07-14"}},  # holiday
    ]}
    cal, http = calendar([(200, page1), (200, page2)])
    busy = cal.busy_intervals("shop@group.calendar.google.com", start, start + timedelta(days=7))
    assert busy[0] == (datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc), datetime(2026, 7, 11, 14, 0, tzinfo=timezone.utc))
    assert busy[1] == (datetime(2026, 7, 13, 0, 0, tzinfo=TZ), datetime(2026, 7, 14, 0, 0, tzinfo=TZ))
    assert len(busy) == 2
    q = parse_qs(urlparse(http.requests[0]["uri"]).query)
    assert q["singleEvents"] == ["true"] and q["timeMin"] == [start.isoformat()]
    assert "shop%40group.calendar.google.com" in http.requests[0]["uri"]
    assert parse_qs(urlparse(http.requests[1]["uri"]).query)["pageToken"] == ["p2"]


def test_create_event_sends_times_with_timezone():
    cal, http = calendar([(200, {"id": "evt_abc"})])
    s = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    event_id = cal.create_event("cal-id", s, s + timedelta(hours=2), "HVAC visit: Jordan Lee",
                                "Phone: +19195550142", "America/New_York")
    assert event_id == "evt_abc"
    body = json.loads(http.requests[0]["body"])
    assert http.requests[0]["method"] == "POST"
    assert body["start"] == {"dateTime": "2026-07-11T12:00:00+00:00", "timeZone": "America/New_York"}
    assert body["summary"] == "HVAC visit: Jordan Lee" and "+19195550142" in body["description"]


def test_calendar_permission_error_surfaces():
    from googleapiclient.errors import HttpError
    cal, _ = calendar([(404, {"error": {"code": 404, "message": "Not Found"}})])
    s = datetime(2026, 7, 11, tzinfo=TZ)
    with pytest.raises(HttpError):
        cal.busy_intervals("not-shared@group.calendar.google.com", s, s + timedelta(days=1))


def test_bad_service_account_value_gives_clear_error():
    with pytest.raises(ValueError, match="GOOGLE_SERVICE_ACCOUNT_JSON_B64"):
        GoogleCalendar("not base64 json")


# ---------------- Slack and email alerts ----------------

def test_slack_alert_posts_text(monkeypatch):
    posts = []
    monkeypatch.setattr("app.adapters.httpx.post", lambda url, json, timeout: posts.append((url, json)))
    AlertSender(SimpleNamespace(smtp_host="")).send(
        {"slack_webhook_url": "https://hooks.slack.com/services/T/B/X"}, "Lead needs a person", "Details", urgent=True)
    url, payload = posts[0]
    assert url.startswith("https://hooks.slack.com/") and payload["text"].startswith(":rotating_light: URGENT *Lead needs a person*")


def test_email_alert_uses_starttls_and_login(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen["server"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): seen["tls"] = True
        def login(self, u, p): seen["login"] = u
        def send_message(self, msg): seen["msg"] = msg

    monkeypatch.setattr("app.adapters.smtplib.SMTP", FakeSMTP)
    s = SimpleNamespace(smtp_host="smtp.example.com", smtp_port=587, smtp_user="bot@example.com",
                        smtp_password="pw", alert_from_email="alerts@example.com")
    AlertSender(s).send({"owner_email": "owner@example.com"}, "New booking", "Sat 8-10am")
    assert seen["server"] == ("smtp.example.com", 587) and seen["tls"] and seen["login"] == "bot@example.com"
    assert seen["msg"]["To"] == "owner@example.com" and seen["msg"]["Subject"] == "New booking"


def test_alert_failure_never_crashes_the_agent(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr("app.adapters.httpx.post", boom)
    AlertSender(SimpleNamespace(smtp_host="")).send({"slack_webhook_url": "https://hooks.slack.com/x"}, "t", "x")
