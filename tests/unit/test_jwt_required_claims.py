"""A token missing a claim the application relies on is refused like any
other invalid token.

PyJWT checks `exp` only when the claim is present; a signed token without
it never expires. jwt.decode(..., options={"require": [...]}) makes the
claim mandatory (PyJWT usage docs, "Requiring Presence of Claims"). Each
test signs a token with the application's key, drops one claim, and expects
the same refusal an unsigned or expired token gets.
"""

import asyncio
import time
import uuid

import jwt
import pytest

from app.core import database as database_module
from app.core.config import settings
from app.core.rate_limit import user_id_from_token
from app.core.security import (
    RefreshTokenError,
    create_access_token,
    create_refresh_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_password_hash,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_email_token,
    verify_password_reset_token,
)
from app.models.user import User


def _without(token, claim):
    claims = jwt.decode(
        token, settings.JWT_SECRET_KEY.get_secret_value(),
        algorithms=[settings.JWT_ALGORITHM], options={"verify_exp": False},
    )
    del claims[claim]
    return jwt.encode(
        claims, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )


def _user(db_session):
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=get_password_hash("Str0ngPass1"),
        gdpr_consent=True,
        privacy_policy_accepted=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


# jti is what logout revokes an access token by: a signed token without one
# could never be revoked, so it is not accepted.
@pytest.mark.parametrize("claim", ["exp", "sub", "type", "jti"])
def test_an_access_token_missing_a_claim_is_401(client, db_session, claim):
    user = _user(db_session)
    token = _without(create_access_token(user.id), claim)

    response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_the_untouched_access_token_is_accepted(client, db_session):
    user = _user(db_session)

    response = client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {create_access_token(user.id)}"}
    )

    assert response.status_code == 200


@pytest.mark.parametrize("claim", ["exp", "sub", "type", "jti"])
def test_the_rate_limiter_does_not_count_an_access_token_missing_a_claim(claim):
    token = _without(create_access_token(7), claim)

    assert user_id_from_token(_Request(token)) is None


class _Request:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}


@pytest.mark.parametrize("claim", ["exp", "sub", "type", "jti", "family"])
def test_a_refresh_token_missing_a_claim_is_refused_as_invalid(db_session, claim):
    user = _user(db_session)
    token = _without(create_refresh_token(user.id), claim)

    async def scenario():
        async with database_module.AsyncSessionLocal() as db:
            return await rotate_refresh_token(token, db)

    with pytest.raises(RefreshTokenError) as refused:
        asyncio.run(scenario())

    # "type" is the one with its own, more specific message; it is refused too.
    assert str(refused.value) in {"Could not validate refresh token", "Not a refresh token"}
    if claim != "type":
        assert str(refused.value) == "Could not validate refresh token"


def test_a_refresh_token_missing_exp_is_refused_even_when_registered(fake_redis, db_session):
    user = _user(db_session)
    token = asyncio.run(_issue(user.id))
    forged = _without(token, "exp")

    async def scenario():
        async with database_module.AsyncSessionLocal() as db:
            return await rotate_refresh_token(forged, db)

    with pytest.raises(RefreshTokenError, match="Could not validate refresh token"):
        asyncio.run(scenario())


async def _issue(user_id):
    from app.core.security import issue_refresh_token
    return await issue_refresh_token(user_id)


@pytest.mark.parametrize("claim", ["exp", "sub", "type", "pwd"])
def test_a_reset_token_missing_a_claim_is_refused(claim):
    token = _without(generate_password_reset_token("jane@example.com", "hash"), claim)

    assert verify_password_reset_token(token) is None


@pytest.mark.parametrize("claim", ["exp", "sub", "type"])
def test_a_verification_token_missing_a_claim_is_refused(claim):
    token = _without(generate_email_verification_token("jane@example.com"), claim)

    assert verify_email_token(token) is None


def test_untouched_reset_and_verification_tokens_are_accepted():
    assert verify_password_reset_token(generate_password_reset_token("jane@example.com", "hash"))
    assert verify_email_token(generate_email_verification_token("jane@example.com")) == "jane@example.com"


def test_logout_revocation_of_a_refresh_token_missing_exp_revokes_nothing(fake_redis):
    # revoke_refresh_token ignores expiry but still wants a token of ours.
    token = jwt.encode(
        {"sub": "42", "type": "refresh", "jti": "j1", "family": "f1", "iat": time.time()},
        settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM,
    )
    fake_redis.sadd("refresh_family:f1", "j1")
    fake_redis.set("refresh_active:j1", "{}")

    assert asyncio.run(revoke_refresh_token(token)) is True

    assert fake_redis.exists("refresh_family:f1", "refresh_active:j1") == 2
