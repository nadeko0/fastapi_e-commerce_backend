"""Input schemas for accounts (app/schemas/user.py), property by property."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.schemas.user import PasswordReset, UserCreate, normalize_email

# Local parts and domains that email-validator accepts, in mixed case.
_local = st.from_regex(r"[A-Za-z][A-Za-z0-9._]{0,15}[A-Za-z0-9]", fullmatch=True).filter(
    lambda s: ".." not in s
)
_domain = st.from_regex(r"[A-Za-z][A-Za-z0-9]{1,10}\.(com|org|NET|De)", fullmatch=True)
_emails = st.builds(lambda local, domain: f"{local}@{domain}", _local, _domain)
_blanks = st.sampled_from(["", " ", "  ", "\t"])


def _registration(**overrides):
    return {
        "email": "jane@example.com",
        "password": "Str0ngPass",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        **overrides,
    }


@given(st.text())
def test_normalize_email_is_idempotent(text):
    once = normalize_email(text)
    assert normalize_email(once) == once
    assert once == once.strip()


@given(_emails, _blanks, _blanks)
def test_every_spelling_of_an_address_registers_as_the_same_email(email, before, after):
    spellings = {email, email.upper(), email.lower(), email.swapcase(), f"{before}{email}{after}"}
    stored = {UserCreate(**_registration(email=spelling)).email for spelling in spellings}
    assert stored == {email.lower()}


@given(_emails)
def test_a_reset_request_is_normalised_like_a_registration(email):
    assert PasswordReset(email=email.upper()).email == UserCreate(
        **_registration(email=email)
    ).email


@pytest.mark.parametrize("email", ["not-an-email", "", "a@", "@example.com", None, 42])
def test_what_is_not_an_email_is_still_refused(email):
    with pytest.raises(ValidationError):
        UserCreate(**_registration(email=email))
