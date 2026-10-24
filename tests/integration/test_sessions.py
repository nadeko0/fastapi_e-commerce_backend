"""Sessions end to end: login, the tokens it hands out, refresh rotation,
logout and password reset - the edge cases tests/integration/test_auth.py
does not already walk through."""

import asyncio
import time
from datetime import timedelta

import jwt
import pytest
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st

from app.core import security
from app.core.config import settings
from app.core.security import (
    blacklist_token,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    issue_refresh_token,
    revoke_refresh_token,
    user_id_from_subject,
)
from app.models.user import User
from app.services.redis import RedisService

USERS = "/api/v1/users"
EMAIL = "session.user@example.com"
PASSWORD = "Str0ngPass1"


def _register(client, email=EMAIL):
    client.post(f"{USERS}/register", json={
        "email": email, "password": PASSWORD, "full_name": "Session User",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


def _tokens(client, email=EMAIL):
    _register(client, email)
    return _login(client, email).json()["data"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _user(db_session, email=EMAIL):
    db_session.expire_all()
    return db_session.query(User).filter(User.email == email).one()


# --- login ----------------------------------------------------------------


def test_every_kind_of_refusal_looks_the_same_and_costs_one_bcrypt(
    client, db_session, monkeypatch
):
    _register(client)
    _register(client, "inactive@example.com")
    inactive = _user(db_session, "inactive@example.com")
    inactive.is_active = False
    db_session.commit()
    calls = []
    original = security.verify_password_async

    async def counting(*args):
        calls.append(args)
        return await original(*args)

    monkeypatch.setattr("app.api.v1.users.verify_password_async", counting)

    answers = [
        _login(client, EMAIL, "Wr0ngPassword"),
        _login(client, "nobody@example.com", PASSWORD),
        _login(client, "inactive@example.com", PASSWORD),
    ]

    assert {response.status_code for response in answers} == {401}
    assert len({response.text for response in answers}) == 1
    assert len(calls) == 3


def test_login_records_when_it_happened(client, db_session):
    _register(client)
    assert _user(db_session).last_login is None

    with freeze_time("2026-03-01 08:00:00"):
        assert _login(client).status_code == 200

    assert _user(db_session).last_login.isoformat() == "2026-03-01T08:00:00+00:00"


@pytest.mark.parametrize("form", [{"username": EMAIL}, {"password": PASSWORD}, {}])
def test_login_without_both_fields_is_422(client, form):
    assert client.post(f"{USERS}/login", data=form).status_code == 422


def test_the_tokens_handed_out_are_what_they_say(client, db_session):
    tokens = _tokens(client)
    user_id = _user(db_session).id
    access = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    refresh = jwt.decode(tokens["refresh_token"], options={"verify_signature": False})

    assert tokens["token_type"] == "bearer"
    assert (access["sub"], access["type"]) == (str(user_id), "access")
    # iat has sub-second precision, exp is whole seconds.
    assert access["exp"] - int(access["iat"]) == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert (refresh["sub"], refresh["type"]) == (str(user_id), "refresh")
    assert refresh["exp"] - int(refresh["iat"]) == settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert refresh["jti"] and refresh["family"] and access["jti"]


# --- token claims (properties) --------------------------------------------


@given(st.integers(min_value=1, max_value=2**31 - 1))
def test_an_access_token_names_its_user_and_nobody_else(user_id):
    payload = jwt.decode(
        create_access_token(user_id), settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM]
    )
    assert user_id_from_subject(payload["sub"]) == user_id


@given(st.integers(min_value=2**31))
def test_ids_the_column_cannot_hold_name_nobody(user_id):
    assert user_id_from_subject(str(user_id)) is None


@given(st.one_of(st.integers(), st.none(), st.lists(st.integers()), st.floats()))
def test_a_subject_that_is_not_text_names_nobody(subject):
    assert user_id_from_subject(subject) is None


def test_two_access_tokens_for_one_user_are_two_tokens():
    # Each carries its own jti, so revoking one leaves the other alone.
    assert create_access_token(1) != create_access_token(1)


# --- refresh --------------------------------------------------------------


def test_a_rotated_refresh_token_stays_in_its_family_and_works_once(client):
    tokens = _tokens(client)
    first = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})
    rotated = first.json()["data"]["refresh_token"]

    claims = [
        jwt.decode(token, options={"verify_signature": False})
        for token in (tokens["refresh_token"], rotated)
    ]
    assert claims[0]["family"] == claims[1]["family"]
    assert claims[0]["jti"] != claims[1]["jti"]
    second = client.post(f"{USERS}/refresh", json={"refresh_token": rotated})
    assert second.status_code == 200
    assert client.get(f"{USERS}/me", headers=_bearer(second.json()["data"]["access_token"])).status_code == 200


def test_a_refresh_token_issued_before_a_password_reset_is_refused(client, db_session):
    with freeze_time("2026-01-01 12:00:00") as clock:
        tokens = _tokens(client)
        clock.tick(timedelta(seconds=2))
        user = _user(db_session)
        reset = client.post(
            f"{USERS}/password/reset/confirm",
            json={"token": generate_password_reset_token(user.email, user.hashed_password), "current_password": "unused", "new_password": "NewStr0ngPass"},
        )
        assert reset.status_code == 200

        response = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 401


def test_a_refresh_token_of_a_deleted_account_is_refused(client, db_session):
    tokens = _tokens(client)
    db_session.delete(_user(db_session))
    db_session.commit()

    response = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["jti", "family"])
def test_a_refresh_token_without_its_chain_claims_is_refused(client, db_session, missing):
    _register(client)
    claims = {
        "sub": str(_user(db_session).id), "type": "refresh", "jti": "j", "family": "f",
        "exp": int(time.time()) + 600,
    }
    del claims[missing]
    token = jwt.encode(claims, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)

    assert client.post(f"{USERS}/refresh", json={"refresh_token": token}).status_code == 401


def test_a_refresh_token_never_registered_burns_nothing_else(client, db_session):
    tokens = _tokens(client)
    stray = asyncio.run(issue_refresh_token(_user(db_session).id))
    # Forget it, as if it had expired out of Redis.
    RedisService().consume_refresh_token(
        jwt.decode(stray, options={"verify_signature": False})["jti"]
    )

    assert client.post(f"{USERS}/refresh", json={"refresh_token": stray}).status_code == 401
    # Its own family was burnt; the session from the login is another one.
    assert client.post(
        f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).status_code == 200


# --- logout ---------------------------------------------------------------


def test_a_logged_out_token_cannot_log_out_again(client):
    tokens = _tokens(client)
    assert client.post(f"{USERS}/logout", headers=_bearer(tokens["access_token"])).status_code == 200

    again = client.post(f"{USERS}/logout", headers=_bearer(tokens["access_token"]))

    assert again.status_code == 401


@pytest.mark.parametrize("refresh_token", ["garbage", "a.b.c"])
def test_logout_with_a_refresh_token_that_is_not_one_still_logs_out(client, refresh_token):
    tokens = _tokens(client)

    response = client.post(
        f"{USERS}/logout", json={"refresh_token": refresh_token}, headers=_bearer(tokens["access_token"])
    )

    assert response.status_code == 200
    assert client.get(f"{USERS}/me", headers=_bearer(tokens["access_token"])).status_code == 401


def test_revoking_a_signed_token_without_a_family_is_a_no_op():
    token = jwt.encode(
        {"sub": "1", "type": "refresh", "exp": int(time.time()) + 60},
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    assert asyncio.run(revoke_refresh_token(token)) is True


def test_logout_needs_a_login(client):
    assert client.post(f"{USERS}/logout").status_code == 401


def test_an_access_token_that_expired_on_the_way_is_still_blacklisted(fake_redis):
    # logout reads the remaining lifetime from `exp`; for a token that has
    # just expired that is nothing, and it is blacklisted for a second.
    with freeze_time("2026-01-01 12:00:00") as clock:
        token = create_access_token(1)
        clock.tick(timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES, seconds=1))
        asyncio.run(security.logout(token, "garbage"))

        jti = jwt.decode(token, options={"verify_signature": False})["jti"]
        assert fake_redis.get(f"blacklist:jti:{jti}") == "1"
        assert 0 < fake_redis.ttl(f"blacklist:jti:{jti}") <= 1


def test_a_token_that_does_not_verify_is_not_blacklisted(fake_redis):
    assert asyncio.run(blacklist_token("not.a.token", 60)) is False
    assert fake_redis.keys("blacklist:*") == []


# --- password reset -------------------------------------------------------


def test_an_expired_reset_link_is_refused(client, db_session):
    _register(client)
    user = _user(db_session)
    with freeze_time("2026-01-01 12:00:00") as clock:
        token = generate_password_reset_token(user.email, user.hashed_password)
        clock.tick(timedelta(hours=24, seconds=1))
        check = client.post(f"{USERS}/password/reset-token/validate", json={"token": token})
        reset = client.post(
            f"{USERS}/password/reset/confirm",
            json={"token": token, "current_password": "unused", "new_password": "NewStr0ngPass"},
        )

    assert (check.status_code, reset.status_code) == (400, 400)


def test_a_verification_token_is_not_a_reset_token(client):
    _register(client)
    token = generate_email_verification_token(EMAIL)

    assert client.post(f"{USERS}/password/reset-token/validate", json={"token": token}).status_code == 400
    assert client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "current_password": "unused", "new_password": "NewStr0ngPass"},
    ).status_code == 400


def test_a_reset_link_for_an_account_that_is_gone_is_404(client):
    token = generate_password_reset_token("gone@example.com", "$2b$04$whatever")

    assert client.post(f"{USERS}/password/reset-token/validate", json={"token": token}).status_code == 404
    assert client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "current_password": "unused", "new_password": "NewStr0ngPass"},
    ).status_code == 404


def test_a_reset_with_a_weak_new_password_is_422_and_spends_nothing(client, db_session):
    _register(client)
    user = _user(db_session)
    token = generate_password_reset_token(user.email, user.hashed_password)

    weak = client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "current_password": "unused", "new_password": "weak"},
    )

    assert weak.status_code == 422
    assert client.post(f"{USERS}/password/reset-token/validate", json={"token": token}).status_code == 200


def test_a_reset_request_for_an_unknown_email_looks_like_any_other(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_password_reset_email", lambda *args: sent.append(args)
    )
    _register(client)

    known = client.post(f"{USERS}/password/reset", json={"email": EMAIL})
    unknown = client.post(f"{USERS}/password/reset", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json()["data"] == unknown.json()["data"]
    assert len(sent) == 1
