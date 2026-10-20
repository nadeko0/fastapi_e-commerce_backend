"""Tokens issued before the move from python-jose to PyJWT keep working.

Access tokens live minutes, but refresh, password-reset and e-mail
verification tokens live days and are in the wild during a deploy. The two
tokens below were produced once by python-jose 3.5.0 (jwt.encode, HS256)
for LEGACY_SECRET and an `exp` in 2100; python-jose itself is not a
dependency any more and is not needed to run these tests.
"""
import time

import jwt

from app.core.config import settings
from app.core.security import (
    revoke_refresh_token,
    validate_token,
    verify_password_reset_token,
)

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


def test_an_access_token_issued_by_python_jose_still_validates(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", LEGACY_SECRET)

    assert validate_token(LEGACY_ACCESS_TOKEN) == LEGACY_ACCESS_CLAIMS


def test_a_reset_token_issued_by_python_jose_still_resolves_to_its_email(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", LEGACY_SECRET)

    assert verify_password_reset_token(LEGACY_RESET_TOKEN) == "legacy@example.com"


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
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    fake_redis.sadd("refresh_family:f1", "j1")
    fake_redis.set("refresh_active:j1", "{}")

    revoke_refresh_token(expired)

    assert not fake_redis.exists("refresh_family:f1", "refresh_active:j1")
