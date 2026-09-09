"""Management CLI, for the tasks with no place in the web UI.

There is no sign-up route, so the first admin is created here::

    python -m app.cli create-admin --email admin@example.com --password secret \
        --first-name Admin --last-name User
"""

import argparse
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Role, User
from app.security import hash_password, is_valid_email, normalize_email


def create_admin(email: str, password: str, first_name: str, last_name: str) -> int:
    """Create an admin account.

    Args:
        email: Address to sign in with; must not already be registered.
        password: Plain password, hashed before storage.
        first_name: Given name.
        last_name: Family name.

    Returns:
        A process exit code: 0 on success, 1 with a message on stderr when the
        address is invalid or taken.
    """
    email = normalize_email(email)
    if not is_valid_email(email):
        print(f"'{email}' is not a valid email address.", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
            print(f"A user with email {email} already exists.", file=sys.stderr)
            return 1
        db.add(
            User(
                email=email,
                password_hash=hash_password(password),
                first_name=first_name,
                last_name=last_name,
                role=Role.ADMIN,
            )
        )
        db.commit()
    print(f"Admin user {email} created.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the chosen command.

    Args:
        argv: Arguments to parse; defaults to the process's own.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(prog="app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    admin_parser = subparsers.add_parser("create-admin", help="Create an admin user")
    admin_parser.add_argument("--email", required=True)
    admin_parser.add_argument("--password", required=True)
    admin_parser.add_argument("--first-name", required=True)
    admin_parser.add_argument("--last-name", required=True)

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        return create_admin(args.email, args.password, args.first_name, args.last_name)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
