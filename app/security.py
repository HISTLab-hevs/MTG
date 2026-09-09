"""Password hashing, email validation and the signed session cookie.

The session is a signed, timestamped token holding only the user id: there is no
server-side session store, and the idle timeout is the token's own max age.
"""

import re

import bcrypt
from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings

SESSION_COOKIE = "mtg_session"


_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@"          # local part
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"  # one+ sub-domains
    r"[A-Za-z]{2,63}$"               # TLD
)


def normalize_email(email: str) -> str:
    """Canonical form stored and compared: trimmed and lower-cased."""
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    """Whether an address is plausible, normalized or not.

    Rejects a missing domain or TLD, spaces, doubled or trailing dots, and an
    over-long address, without rejecting the unusual but valid forms
    universities use.
    """
    candidate = normalize_email(email)
    if not candidate or len(candidate) > 254 or ".." in candidate:
        return False
    return bool(_EMAIL_RE.match(candidate))


def session_max_age() -> int:
    """Idle timeout in seconds; tokens older than this are rejected."""
    return get_settings().session_idle_minutes * 60


def hash_password(password: str) -> str:
    """Hash a password for storage, with a per-password salt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash, False on a malformed one."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def _serializer() -> URLSafeTimedSerializer:
    """The signer for session tokens, salted so it signs nothing else."""
    return URLSafeTimedSerializer(get_settings().app_secret_key, salt="mtg-session")


def create_session_token(user_id: int) -> str:
    """Sign a session token carrying one account id."""
    return _serializer().dumps({"uid": user_id})


def read_session_token(token: str) -> int | None:
    """The account id in a token, or None when it is invalid or expired."""
    try:
        return _serializer().loads(token, max_age=session_max_age())["uid"]
    except Exception:
        return None


def set_session_cookie(response, user_id: int) -> None:
    """Write the session cookie, refreshing the idle timeout."""
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user_id),
        max_age=session_max_age(),
        httponly=True,
        samesite="lax",
        secure=get_settings().https_only,
    )
