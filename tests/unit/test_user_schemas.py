"""Input schemas for accounts (app/schemas/user.py), property by property."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.schemas.user import PasswordReset, PasswordUpdate, UserCreate, normalize_email

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


# --- password policy ------------------------------------------------------


@pytest.mark.parametrize(
    "password,reason",
    [
        ("Sh0rt", "at least 8 characters"),
        ("onlyletters", "a digit"),
        ("12345678", "a letter"),
        ("With space1", "a character outside the allowed set"),
        ("Pässwörd12", "a non-ASCII letter"),
        ("A1" + "a" * 71, "more than 72 bytes"),
    ],
)
def test_weak_or_unusable_passwords_are_refused(password, reason):
    with pytest.raises(ValidationError):
        UserCreate(**_registration(password=password))
    with pytest.raises(ValidationError):
        PasswordUpdate(current_password="x", new_password=password)


@pytest.mark.parametrize("password", ["Str0ngPass", "a1@$!%*#?&", "A1" + "a" * 70])
def test_good_passwords_are_accepted(password):
    assert UserCreate(**_registration(password=password)).password == password
    assert PasswordUpdate(current_password="x", new_password=password).new_password == password


# ASCII only, [0-9] and not \d: the schema's pattern is matched with
# Unicode-aware \d, so non-ASCII digits pass it, and a password made of
# them can exceed 72 bytes well under 72 characters - a different property
# from the one this test is about.
@given(st.from_regex(r"[A-Za-z0-9@$!%*#?&]{8,72}", fullmatch=True))
def test_the_policy_is_letters_and_digits_both_present(password):
    has_both = any(c.isalpha() for c in password) and any(c.isdigit() for c in password)
    try:
        UserCreate(**_registration(password=password))
    except ValidationError:
        assert not has_both
    else:
        assert has_both


@pytest.mark.parametrize("consent", ["gdpr_consent", "privacy_policy_accepted"])
def test_registration_needs_both_consents(consent):
    with pytest.raises(ValidationError):
        UserCreate(**_registration(**{consent: False}))
