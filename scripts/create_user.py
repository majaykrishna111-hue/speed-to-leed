"""Create a staff login, or reset an existing one's password.

Business owner (sees only that business's leads):
    python scripts/create_user.py owner@example.com --client triangle-comfort --name "Mike"
You, the operator (sees every business):
    python scripts/create_user.py you@example.com --operator

You'll be asked to type a password (10+ characters). Add --generate to get a random one printed.
Run it with the production DATABASE_URL set (e.g. in Render's Shell tab).
"""
import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models import Client, StaffUser, make_session_factory  # noqa: E402
from app.staff import MIN_PASSWORD_LENGTH, hash_password  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--client", help="slug of the business this owner belongs to")
    who.add_argument("--operator", action="store_true", help="can see every business")
    ap.add_argument("--name", default="")
    ap.add_argument("--generate", action="store_true", help="generate and print a random password")
    ap.add_argument("--deactivate", action="store_true", help="turn this login off")
    args = ap.parse_args()

    sf = make_session_factory(get_settings().database_url)
    email = args.email.strip().lower()
    with sf() as db:
        client_id = None
        if args.client:
            client = db.scalar(select(Client).where(Client.slug == args.client))
            if client is None:
                sys.exit(f"No business with slug '{args.client}'. Add it first with scripts/upsert_client.py.")
            client_id = client.id
        user = db.scalar(select(StaffUser).where(StaffUser.email == email))
        if args.deactivate:
            if user is None:
                sys.exit("No such user.")
            user.active = False
            db.commit()
            print(f"Deactivated {email}.")
            return
        if args.generate:
            password = secrets.token_urlsafe(12)
        else:
            password = getpass.getpass(f"New password for {email} ({MIN_PASSWORD_LENGTH}+ characters): ")
            if password != getpass.getpass("Type it again: "):
                sys.exit("Passwords didn't match.")
        try:
            hashed = hash_password(password)
        except ValueError as exc:
            sys.exit(str(exc))
        if user is None:
            user = StaffUser(email=email)
            db.add(user)
        user.password_hash, user.client_id, user.active = hashed, client_id, True
        if args.name:
            user.name = args.name
        db.commit()
        scope = "every business (operator)" if client_id is None else f"business '{args.client}'"
        print(f"Saved login {email} for {scope}.")
        if args.generate:
            print(f"Password: {password}\nShare it privately and ask them to keep it safe.")


if __name__ == "__main__":
    main()
