"""Sessions end to end: login, the tokens it hands out, refresh rotation,
logout and password reset - the edge cases tests/integration/test_auth.py
does not already walk through."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

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
    return _login(client, email).json()


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
    # The request id is the one member that differs between requests.
    assert len({response.json()["detail"] for response in answers}) == 1
    assert len({response.json()["title"] for response in answers}) == 1
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
    # The login starts the family: its time is the family's start.
    assert refresh["auth_time"] == refresh["iat"]


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
    rotated = first.json()["refresh_token"]

    claims = [
        jwt.decode(token, options={"verify_signature": False})
        for token in (tokens["refresh_token"], rotated)
    ]
    assert claims[0]["family"] == claims[1]["family"]
    assert claims[0]["jti"] != claims[1]["jti"]
    second = client.post(f"{USERS}/refresh", json={"refresh_token": rotated})
    assert second.status_code == 200
    assert client.get(f"{USERS}/me", headers=_bearer(second.json()["access_token"])).status_code == 200


def test_a_refresh_token_issued_before_a_password_reset_is_refused(client, db_session):
    with freeze_time("2026-01-01 12:00:00") as clock:
        tokens = _tokens(client)
        clock.tick(timedelta(seconds=2))
        user = _user(db_session)
        reset = client.post(
            f"{USERS}/password/reset/confirm",
            json={"token": generate_password_reset_token(user.email, user.hashed_password), "new_password": "NewStr0ngPass"},
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


# --- absolute session lifetime (REFRESH_SESSION_MAX_DAYS) -----------------
# Rotated every few days, a family would otherwise live for ever: each new
# token gets REFRESH_TOKEN_EXPIRE_DAYS of its own.

DAY = 86400
T0 = "2026-01-01 12:00:00"


def _claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def _refresh(client, token):
    return client.post(f"{USERS}/refresh", json={"refresh_token": token})


def _rotate_every(client, clock, token, days, times):
    """Refresh `times` times, `days` apart; the last refresh token."""
    for _ in range(times):
        clock.tick(timedelta(days=days))
        response = _refresh(client, token)
        assert response.status_code == 200, response.text
        token = response.json()["refresh_token"]
    return token


def _without_request_id(response):
    return {k: v for k, v in response.json().items() if k != "request_id"}


def test_a_session_rotated_often_still_ends_at_its_maximum_age(client, fake_redis):
    max_age = settings.REFRESH_SESSION_MAX_DAYS * DAY
    with freeze_time(T0) as clock:
        login = _tokens(client)["refresh_token"]
        started = _claims(login)["auth_time"]
        # Six days apart, four times: day 24, when a full lifetime would
        # reach past the family's end.
        near = _rotate_every(client, clock, login, days=6, times=4)
        claims = _claims(near)
        assert claims["auth_time"] == started
        assert claims["exp"] == int(started) + max_age
        assert claims["exp"] - int(claims["iat"]) < settings.REFRESH_TOKEN_EXPIRE_DAYS * DAY
        # The Redis entries live as long as the token, not a full lifetime.
        for key in (f"refresh_active:{claims['jti']}", f"refresh_family:{claims['family']}"):
            assert 0 < fake_redis.ttl(key) <= claims["exp"] - int(claims["iat"])

        # A second before the end it still works, and the new token ends then too.
        clock.move_to(datetime.fromtimestamp(int(started) + max_age - 1, UTC))
        last = _refresh(client, near)
        assert last.status_code == 200
        assert _claims(last.json()["refresh_token"])["exp"] == int(started) + max_age

        clock.move_to(datetime.fromtimestamp(int(started) + max_age, UTC))
        ended = _refresh(client, last.json()["refresh_token"])

    # Refused exactly like an expired token.
    with freeze_time(T0) as clock:
        expired = _tokens(client, "expired.session@example.com")["refresh_token"]
        clock.tick(timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS, seconds=1))
        refused_as_expired = _refresh(client, expired)
    assert ended.status_code == refused_as_expired.status_code == 401
    assert _without_request_id(ended) == _without_request_id(refused_as_expired)
    assert ended.headers["WWW-Authenticate"] == refused_as_expired.headers["WWW-Authenticate"]


def test_the_age_is_checked_even_for_a_token_that_has_not_expired(client, monkeypatch):
    # A token can outlive its family when REFRESH_SESSION_MAX_DAYS is
    # lowered after it was issued: its own exp is not the end.
    with freeze_time(T0) as clock:
        token = _tokens(client)["refresh_token"]
        monkeypatch.setattr(settings, "REFRESH_SESSION_MAX_DAYS", settings.REFRESH_TOKEN_EXPIRE_DAYS - 1)
        clock.tick(timedelta(days=settings.REFRESH_SESSION_MAX_DAYS))

        response = _refresh(client, token)

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate refresh token"


def _registered_old_style_refresh_token(user_id, **claims):
    """A refresh token as issued before auth_time existed, registered in Redis."""
    issued_at = time.time()
    payload = {
        "exp": int(issued_at) + settings.REFRESH_TOKEN_EXPIRE_DAYS * DAY,
        "iat": issued_at,
        "sub": str(user_id),
        "type": "refresh",
        "jti": "old-jti",
        "family": "old-family",
        **claims,
    }
    RedisService().register_refresh_token(
        "old-jti", user_id, "old-family", settings.REFRESH_TOKEN_EXPIRE_DAYS * DAY
    )
    return jwt.encode(payload, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)


def test_an_old_token_without_auth_time_counts_from_its_own_iat(client, db_session):
    max_age = settings.REFRESH_SESSION_MAX_DAYS * DAY
    with freeze_time(T0) as clock:
        _register(client)
        old = _registered_old_style_refresh_token(_user(db_session).id)
        issued = _claims(old)["iat"]

        rotated = _rotate_every(client, clock, old, days=6, times=1)
        assert _claims(rotated)["auth_time"] == issued

        near = _rotate_every(client, clock, rotated, days=6, times=3)
        assert _claims(near)["exp"] == int(issued) + max_age
        clock.move_to(datetime.fromtimestamp(int(issued) + max_age, UTC))

        assert _refresh(client, near).status_code == 401


def test_an_old_token_without_auth_time_or_iat_is_refused(client, db_session):
    _register(client)
    old = _registered_old_style_refresh_token(_user(db_session).id)
    claims = _claims(old)
    del claims["iat"]
    no_start = jwt.encode(claims, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)

    assert _refresh(client, no_start).status_code == 401


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
            json={"token": token, "new_password": "NewStr0ngPass"},
        )

    assert (check.status_code, reset.status_code) == (400, 400)


def test_a_verification_token_is_not_a_reset_token(client):
    _register(client)
    token = generate_email_verification_token(EMAIL)

    assert client.post(f"{USERS}/password/reset-token/validate", json={"token": token}).status_code == 400
    assert client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "new_password": "NewStr0ngPass"},
    ).status_code == 400


def test_a_reset_link_for_an_account_that_is_gone_is_404(client):
    token = generate_password_reset_token("gone@example.com", "$2b$04$whatever")

    assert client.post(f"{USERS}/password/reset-token/validate", json={"token": token}).status_code == 404
    assert client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "new_password": "NewStr0ngPass"},
    ).status_code == 404


def test_a_reset_with_a_weak_new_password_is_422_and_spends_nothing(client, db_session):
    _register(client)
    user = _user(db_session)
    token = generate_password_reset_token(user.email, user.hashed_password)

    weak = client.post(
        f"{USERS}/password/reset/confirm",
        json={"token": token, "new_password": "weak"},
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
    assert known.json() == unknown.json()
    assert len(sent) == 1
