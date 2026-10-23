"""One mailbox, one account: emails are compared without regard to case or
surrounding blanks, at registration, login, password reset and email
verification, and the database refuses a second account that differs from
an existing one only in case (a unique index on lower(email))."""

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.security import generate_email_verification_token, get_password_hash
from app.models.user import User

USERS = "/api/v1/users"
PASSWORD = "Str0ngPass1"


def _registration(email):
    return {
        "email": email,
        "password": PASSWORD,
        "full_name": "Case Test",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
    }


def _accounts(db_session, email):
    db_session.expire_all()
    return db_session.query(User).filter(func.lower(User.email) == email.lower()).all()


def _legacy_account(db_session, email):
    """A row as registration stored it before emails were normalised: in
    whatever case the user typed."""
    user = User(
        email=email,
        hashed_password=get_password_hash(PASSWORD),
        full_name="Legacy Spelling",
        gdpr_consent=True,
        privacy_policy_accepted=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_an_email_differing_only_in_case_does_not_make_a_second_account(client, db_session):
    client.post(f"{USERS}/register", json=_registration("case@example.com"))
    client.post(f"{USERS}/register", json=_registration("Case@Example.COM"))
    client.post(f"{USERS}/register", json=_registration("  CASE@example.com  "))

    assert len(_accounts(db_session, "case@example.com")) == 1


def test_registration_stores_the_email_normalised(client, db_session):
    client.post(f"{USERS}/register", json=_registration("  Jane.Doe@Example.COM "))

    (user,) = _accounts(db_session, "jane.doe@example.com")
    assert user.email == "jane.doe@example.com"


@pytest.mark.parametrize(
    "typed", ["jane.doe@example.com", "JANE.DOE@EXAMPLE.COM", "  Jane.Doe@Example.com  "]
)
def test_login_accepts_the_email_in_any_case(client, typed):
    client.post(f"{USERS}/register", json=_registration("jane.doe@example.com"))

    response = client.post(f"{USERS}/login", data={"username": typed, "password": PASSWORD})

    assert response.status_code == 200


def test_an_account_stored_in_mixed_case_can_still_log_in(client, db_session):
    # Rows from before normalisation keep their spelling; lookups compare
    # lower(email), so their owners are not locked out by this change.
    _legacy_account(db_session, "Legacy.User@Example.com")

    for typed in ("legacy.user@example.com", "Legacy.User@Example.com"):
        response = client.post(f"{USERS}/login", data={"username": typed, "password": PASSWORD})
        assert response.status_code == 200, typed


def test_password_reset_finds_the_account_in_any_case(client, db_session, monkeypatch):
    _legacy_account(db_session, "Legacy.User@Example.com")
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_password_reset_email",
        lambda to_email, token: sent.append((to_email, token)),
    )

    client.post(f"{USERS}/password/reset", json={"email": "LEGACY.USER@example.com"})

    assert len(sent) == 1
    # The mail goes to the address on the account, not to the spelling typed.
    assert sent[0][0] == "Legacy.User@Example.com"
    reset = client.post(
        f"{USERS}/password/reset/{sent[0][1]}",
        json={"current_password": "unused", "new_password": "NewStr0ngPass"},
    )
    assert reset.status_code == 200


def test_email_verification_finds_the_account_whatever_case_the_token_carries(
    client, db_session
):
    _legacy_account(db_session, "Legacy.User@Example.com")
    token = generate_email_verification_token("legacy.user@EXAMPLE.com")

    response = client.get(f"{USERS}/verify-email/{token}")

    assert response.status_code == 200
    (user,) = _accounts(db_session, "legacy.user@example.com")
    assert user.is_email_verified is True


def test_the_database_refuses_two_rows_that_differ_only_in_case(db_session):
    # The last line of defence, for rows that do not come through the API.
    _legacy_account(db_session, "dup@example.com")

    with pytest.raises(IntegrityError):
        _legacy_account(db_session, "DUP@example.com")
    db_session.rollback()


NFD = "é@example.com"
NFC = "é@example.com"


def test_canonically_equivalent_emails_are_one_account(client, db_session):
    # UAX #15: "e" + U+0301 and U+00E9 are the same text.
    client.post(f"{USERS}/register", json=_registration(NFD))
    client.post(f"{USERS}/register", json=_registration(NFC))

    (user,) = _accounts(db_session, NFC)
    assert user.email == NFC


@pytest.mark.parametrize("typed", [NFD, NFC, NFD.upper(), NFC.upper()])
def test_login_accepts_either_form_of_a_non_ascii_email(client, typed):
    client.post(f"{USERS}/register", json=_registration(NFC))

    response = client.post(f"{USERS}/login", data={"username": typed, "password": PASSWORD})

    assert response.status_code == 200


def test_password_reset_finds_the_account_by_either_form(client, monkeypatch):
    client.post(f"{USERS}/register", json=_registration(NFC))
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_password_reset_email",
        lambda to_email, token: sent.append(to_email),
    )

    client.post(f"{USERS}/password/reset", json={"email": NFD})

    assert sent == [NFC]
