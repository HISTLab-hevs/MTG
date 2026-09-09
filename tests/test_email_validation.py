"""Email validation: tolerant of real institutional shapes, strict on mistakes."""

import pytest
from sqlalchemy import select

from app.models import User
from app.security import is_valid_email, normalize_email
from tests.conftest import login


@pytest.mark.parametrize("email", [
    "jane@uni.edu",
    "jane+mtg@uni.edu",
    "jane.doe@cs.dept.uni-example.ac.uk",  # deep sub-domains + hyphen
    "a.b_c%d@sub.domain.io",
    "PROF@UNI.EDU",                        # case is normalized, still valid
])
def test_accepts_real_institutional_addresses(email):
    assert is_valid_email(email)


@pytest.mark.parametrize("email", [
    "",
    "no-at-sign",
    "foo@",            # no domain
    "foo@bar",         # no TLD
    "foo@@bar.com",    # doubled @
    "foo..bar@x.com",  # doubled dot in local part
    "foo@bar..com",    # doubled dot in domain
    "foo bar@x.com",   # space
    "@x.com",          # no local part
    "foo@x.c",         # one-letter TLD
    "x" * 250 + "@y.com",  # over length
])
def test_rejects_malformed_addresses(email):
    assert not is_valid_email(email)


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Jane@UNI.edu ") == "jane@uni.edu"


def test_admin_rejects_invalid_email(client, db, admin):
    """The create-user route refuses a bare 'foo@bar' (the old '@'-only check
    would have let it through) and creates no user."""
    login(client, "admin@example.com")
    response = client.post(
        "/admin/users",
        data={
            "email": "foo@bar",
            "password": "password123",
            "first_name": "Bad",
            "last_name": "Email",
        },
    )
    assert response.status_code == 200
    assert "valid email" in response.text.lower()
    assert db.execute(
        select(User).where(User.email == "foo@bar")
    ).scalar_one_or_none() is None
