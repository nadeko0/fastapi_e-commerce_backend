"""A password longer than bcrypt's 72 bytes must be answered like any other
wrong password, never with a 500. bcrypt 5 raises ValueError for such input
instead of truncating it (bcrypt 5.0.0 changelog), and registration and
password change already refuse it with a 422, so no stored password can be
that long: the login and re-confirmation routes only have to say "wrong"."""

import bcrypt
import pytest

from app.core.security import DUMMY_PASSWORD_HASH, verify_password
from app.models.user import User

USERS = "/api/v1/users"
LEGAL = "/api/v1/legal"
EMAIL = "long.password@example.com"
PASSWORD = "Str0ngPass1"

# 73 bytes, 1000 bytes, and 75 bytes of 3-byte characters (25 characters).
TOO_LONG = [
    pytest.param("a" * 73, id="73-ascii"),
    pytest.param("a" * 1000, id="1000-ascii"),
    pytest.param("\u20ac" * 25, id="75-bytes-utf8"),
    pytest.param("\u20ac" * 400, id="1200-bytes-utf8"),
]


@pytest.fixture
def headers(client):
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Long Password",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    response = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _login(client, email, password):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


def test_verify_password_is_false_for_more_than_72_bytes():
    hashed = bcrypt.hashpw(b"x" * 72, bcrypt.gensalt(rounds=4)).decode()

    assert verify_password("x" * 72, hashed) is True
    assert verify_password("x" * 73, hashed) is False
    assert verify_password("\u20ac" * 25, hashed) is False
    assert verify_password("x" * 1000, DUMMY_PASSWORD_HASH) is False


def test_verify_password_still_runs_bcrypt_for_more_than_72_bytes(monkeypatch):
    # Returning at once would make a too-long password measurably faster
    # than a wrong one, and so reveal the limit and the length check.
    calls = []
    real = bcrypt.checkpw

    def spy(password, hashed):
        calls.append(len(password))
        return real(password, hashed)

    monkeypatch.setattr("app.core.security.bcrypt.checkpw", spy)

    assert verify_password("x" * 1000, DUMMY_PASSWORD_HASH) is False
    assert calls == [72]


@pytest.mark.parametrize("password", TOO_LONG)
def test_login_with_a_too_long_password_looks_like_a_wrong_password(
    client, headers, password
):
    wrong = _login(client, EMAIL, "Wr0ngPassword")

    existing = _login(client, EMAIL, password)
    unknown = _login(client, "nobody.here@example.com", password)

    assert wrong.status_code == 401
    assert existing.status_code == wrong.status_code
    assert unknown.status_code == wrong.status_code
    assert existing.json()["detail"] == wrong.json()["detail"]
    assert unknown.json()["detail"] == wrong.json()["detail"]
    assert existing.headers.get("www-authenticate") == wrong.headers.get("www-authenticate")


@pytest.mark.parametrize("password", TOO_LONG)
def test_erasure_with_a_too_long_password_is_answered_like_a_wrong_one(
    client, db_session, headers, password
):
    wrong = client.post(
        f"{LEGAL}/data-request", json={"request_type": "deletion", "password": "Wr0ngPassword"},
        headers=headers,
    )

    response = client.post(
        f"{LEGAL}/data-request", json={"request_type": "deletion", "password": password},
        headers=headers,
    )

    assert wrong.status_code == 401
    assert response.status_code == 401
    assert response.json()["detail"] == wrong.json()["detail"] == "Incorrect password"
    db_session.expire_all()
    assert db_session.query(User).filter(User.email == EMAIL).one().is_active is True


@pytest.mark.parametrize("password", TOO_LONG)
def test_data_request_deletion_with_a_too_long_password_is_answered_like_a_wrong_one(
    client, db_session, headers, password
):
    wrong = client.post(
        f"{LEGAL}/data-request",
        json={"request_type": "deletion", "password": "Wr0ngPassword"}, headers=headers,
    )

    response = client.post(
        f"{LEGAL}/data-request",
        json={"request_type": "deletion", "password": password}, headers=headers,
    )

    assert wrong.status_code == 401
    assert response.status_code == 401
    assert response.json()["detail"] == wrong.json()["detail"] == "Incorrect password"
    db_session.expire_all()
    assert db_session.query(User).filter(User.email == EMAIL).one().is_active is True
