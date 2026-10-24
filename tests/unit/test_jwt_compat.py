"""Tokens issued before the move from python-jose to PyJWT keep working.

Access tokens live minutes, but refresh, password-reset and e-mail
verification tokens live days and are in the wild during a deploy. The two
tokens below were produced once by python-jose 3.5.0 (jwt.encode, HS256)
for LEGACY_SECRET and an `exp` in 2100; python-jose itself is not a
dependency any more and is not needed to run these tests.
"""
import asyncio
import time

import jwt
from pydantic import SecretStr

from app.core.config import settings
from app.core.security import (
    get_password_hash,
    revoke_refresh_token,
    verify_password_reset_token,
)
from app.models.user import User

LEGACY_SECRET = "legacy-python-jose-secret-0123456789"
LEGACY_ACCESS_CLAIMS = {"exp": 4102444800, "iat": 1767225600, "sub": "42", "type": "access"}
LEGACY_ACCESS_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDAsImlhdCI6MTc2NzIyNTYwMCwic3ViIjoiNDIiLCJ0eXBlIjoiYWNjZXNzIn0"
    ".H9voLlTdoPvqppYANxIHP1kzyEC3iJ19GM5d7tkYONk"
)
LEGACY_RESET_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJleHAiOjQxMDI0NDQ4MDAsInN1YiI6ImxlZ2FjeUBleGFtcGxlLmNvbSIsInR5cGUiOiJyZXNldCJ9"
    ".zXkYbgAFd4DIgMt0KaQZPP1cS701tnlYAZqA3CZy1SU"
)


def test_an_access_token_issued_by_python_jose_still_authenticates(
    monkeypatch, client, db_session
):
    # Through the real request path: the user it names (42) exists, and
    # GET /users/me answers as that user.
    db_session.add(User(
        id=42,
        email="legacy@example.com",
        hashed_password=get_password_hash("Str0ngPass1"),
        gdpr_consent=True,
        privacy_policy_accepted=True,
    ))
    db_session.commit()
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", SecretStr(LEGACY_SECRET))
    assert jwt.decode(LEGACY_ACCESS_TOKEN, LEGACY_SECRET, algorithms=["HS256"]) == (
        LEGACY_ACCESS_CLAIMS
    )

    response = client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {LEGACY_ACCESS_TOKEN}"}
    )

    assert response.status_code == 200
    assert response.json()["id"] == 42


def test_a_reset_token_issued_by_python_jose_still_decodes_but_is_no_longer_honoured(
    monkeypatch,
):
    # PyJWT reads it fine. The reset flow refuses it anyway: reset tokens
    # are now bound to the password hash they were issued against, which
    # makes them single-use, and one without that binding could be replayed
    # for its whole lifetime. A fresh reset email is the way forward.
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", SecretStr(LEGACY_SECRET))

    claims = jwt.decode(LEGACY_RESET_TOKEN, LEGACY_SECRET, algorithms=["HS256"])
    assert claims["sub"] == "legacy@example.com"
    assert verify_password_reset_token(LEGACY_RESET_TOKEN) is None


def test_pyjwt_signs_the_same_claims_into_the_same_token():
    # Same header, same claim order, same signature: nothing that compares or
    # stores tokens (the blacklist is keyed by the token string) can tell
    # which library issued one.
    assert jwt.encode(LEGACY_ACCESS_CLAIMS, LEGACY_SECRET, algorithm="HS256") == LEGACY_ACCESS_TOKEN


def test_logout_still_revokes_the_family_of_an_expired_refresh_token(fake_redis):
    # revoke_refresh_token turns expiry checking off ({"verify_exp": False});
    # an option the library did not understand would make this a silent no-op.
    expired = jwt.encode(
        {"exp": int(time.time()) - 60, "sub": "42", "type": "refresh", "jti": "j1", "family": "f1"},
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    fake_redis.sadd("refresh_family:f1", "j1")
    fake_redis.set("refresh_active:j1", "{}")

    asyncio.run(revoke_refresh_token(expired))

    assert not fake_redis.exists("refresh_family:f1", "refresh_active:j1")
