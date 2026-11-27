"""Create or update a client (business) from a JSON file.

    python scripts/upsert_client.py clients/triangle_comfort.json

On the production server (Render -> service -> Shell), paste the JSON instead of committing it,
because a real client's file holds secrets (form_secret, Slack webhook):

    python scripts/upsert_client.py - <<'JSON'
    { ...the client's JSON... }
    JSON
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.flows import normalize_phone  # noqa: E402
from app.models import Client, make_session_factory  # noqa: E402
from app.prompt import STATIC_FIELDS  # noqa: E402

REQUIRED_CONFIG = set(STATIC_FIELDS.values()) | {"service_zips", "slot_windows", "office_hours", "form_secret"}


def main(path: str) -> None:
    data = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    missing = sorted(k for k in REQUIRED_CONFIG if not data["config"].get(k))
    if missing:
        sys.exit(f"Missing config values: {', '.join(missing)}")
    number = normalize_phone(data["twilio_number"])
    if not number:
        sys.exit(f"Invalid twilio_number: {data['twilio_number']}")
    sf = make_session_factory(get_settings().database_url)
    with sf() as db:
        client = db.scalar(select(Client).where(Client.slug == data["slug"]))
        if client is None:
            client = Client(slug=data["slug"])
            db.add(client)
        client.name = data["name"]
        client.twilio_number = number
        client.timezone = data.get("timezone", "America/New_York")
        client.calendar_id = data.get("calendar_id", "")
        client.config = data["config"]
        client.active = data.get("active", True)
        db.commit()
        print(f"Saved client '{client.slug}' (id {client.id}) on {client.twilio_number}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
