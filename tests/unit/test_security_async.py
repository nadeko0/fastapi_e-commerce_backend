"""The async token helpers in app.core.security do what the sync ones do.

Same Redis keys (so a token handled by one is seen by the other), same
fail-closed results when Redis cannot be told, same refresh rotation rules.
The sync versions are exercised by tests/integration/test_sessions.py,
test_auth_dependencies.py, test_jwt_compat.py and test_security_time.py;
this file mirrors those cases for the async twins.
"""

import asyncio
import time
from datetime import timedelta

import fakeredis
import jwt
import pytest
from freezegun import freeze_time

import app.core.database as database_module
import app.services.redis_async as redis_async
from app.core import security
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import (
    RefreshTokenError,
    RevocationUnavailable,
    blacklist_token,
    blacklist_token_async,
    create_access_token,
    create_refresh_token,
    invalidate_tokens_issued_before_now,
    invalidate_tokens_issued_before_now_async,
    issue_refresh_token,
    issue_refresh_token_async,
    logout,
    logout_async,
    revoke_refresh_token,
    revoke_refresh_token_async,
    rotate_refresh_token_async,
)
from app.models.user import User


def _jti(token):
    return jwt.decode(token, options={"verify_signature": False})["jti"]


def _make_user(db_session, email="async.tokens@example.com", **overrides):
    defaults = dict(
        email=email,
        hashed_password="hashed",
        full_name="Async Tokens",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
        created_at=utc_now(),
    )
    defaults.update(overrides)
    user = User(**defaults)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _rotate(token):
    async def scenario():
        async with database_module.AsyncSessionLocal() as db:
            return await rotate_refresh_token_async(token, db)

    return asyncio.run(scenario())


@pytest.fixture
def unreachable(monkeypatch):
    """Redis refuses every connection."""
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(
        redis_async,
        "_create_client",
        lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
    )


# --- issue / rotate -----------------------------------------------------------


def test_issue_registers_the_same_keys_as_the_sync_helper(fake_redis):
    async_token = asyncio.run(issue_refresh_token_async(7))
    sync_token = issue_refresh_token(7)

    for token in (async_token, sync_token):
        payload = jwt.decode(token, options={"verify_signature": False})
        assert payload["type"] == "refresh"
        assert fake_redis.exists(f"refresh_active:{payload['jti']}") == 1
        assert payload["jti"] in fake_redis.smembers(f"refresh_family:{payload['family']}")
        assert payload["family"] in fake_redis.smembers("refresh_user:7")
        assert 0 < fake_redis.ttl(f"refresh_active:{payload['jti']}") <= (
            settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
        )


def test_rotate_returns_a_new_pair_in_the_same_family(db_session, fake_redis):
    user = _make_user(db_session)
    token = asyncio.run(issue_refresh_token_async(user.id))
    family = jwt.decode(token, options={"verify_signature": False})["family"]

    access, refresh = _rotate(token)

    assert jwt.decode(access, options={"verify_signature": False})["sub"] == str(user.id)
    new = jwt.decode(refresh, options={"verify_signature": False})
    assert new["family"] == family
    assert new["jti"] != _jti(token)
    assert fake_redis.exists(f"refresh_active:{_jti(token)}") == 0
    assert fake_redis.exists(f"refresh_active:{new['jti']}") == 1


def test_replaying_a_rotated_token_revokes_the_whole_family(db_session, fake_redis):
    user = _make_user(db_session)
    token = asyncio.run(issue_refresh_token_async(user.id))
    _, newest = _rotate(token)

    with pytest.raises(RefreshTokenError, match="already used"):
        _rotate(token)

    assert fake_redis.exists(f"refresh_active:{_jti(newest)}") == 0
    with pytest.raises(RefreshTokenError):
        _rotate(newest)


def test_a_token_issued_by_the_sync_helper_rotates_through_the_async_one(db_session):
    user = _make_user(db_session)

    access, refresh = _rotate(issue_refresh_token(user.id))

    assert access and refresh


@pytest.mark.parametrize("bad", ["garbage", "a.b.c"])
def test_rotate_refuses_a_token_that_does_not_decode(bad):
    with pytest.raises(RefreshTokenError, match="Could not validate"):
        _rotate(bad)


def test_rotate_refuses_an_access_token():
    with pytest.raises(RefreshTokenError, match="Not a refresh token"):
        _rotate(create_access_token(1))


@pytest.mark.parametrize("missing", ["jti", "family", "sub"])
def test_rotate_refuses_a_token_missing_a_claim(missing):
    claims = {
        "sub": "1",
        "type": "refresh",
        "jti": "j",
        "family": "f",
        "exp": int(time.time()) + 60,
    }
    del claims[missing]
    token = jwt.encode(claims, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(RefreshTokenError, match="Could not validate"):
        _rotate(token)


def test_a_token_that_was_never_registered_is_refused_and_burns_only_its_family(
    db_session, fake_redis
):
    user = _make_user(db_session)
    live = asyncio.run(issue_refresh_token_async(user.id))
    stray = create_refresh_token(user.id)

    with pytest.raises(RefreshTokenError, match="already used or invalid"):
        _rotate(stray)

    assert fake_redis.exists(f"refresh_active:{_jti(live)}") == 1


def test_rotate_refuses_a_token_issued_before_the_password_watermark(db_session):
    user = _make_user(db_session)
    token = asyncio.run(issue_refresh_token_async(user.id))
    time.sleep(0.01)
    assert asyncio.run(invalidate_tokens_issued_before_now_async(user.id)) is True

    with pytest.raises(RefreshTokenError, match="no longer valid"):
        _rotate(token)


def test_rotate_refuses_an_inactive_or_unknown_user(db_session):
    inactive = _make_user(db_session, is_active=False)
    with pytest.raises(RefreshTokenError, match="User not found or inactive"):
        _rotate(asyncio.run(issue_refresh_token_async(inactive.id)))

    with pytest.raises(RefreshTokenError, match="User not found or inactive"):
        _rotate(asyncio.run(issue_refresh_token_async(99999)))


def test_rotate_fails_closed_when_the_watermark_cannot_be_read(db_session, monkeypatch):
    user = _make_user(db_session)
    token = asyncio.run(issue_refresh_token_async(user.id))
    # Redis answers the jti lookup, then cannot be asked about the watermark
    # (is_token_superseded answers True for that: see test_redis_async.py).
    async def unreadable(user_id, issued_at):
        return True

    monkeypatch.setattr(security, "is_token_superseded", unreadable)

    with pytest.raises(RefreshTokenError, match="no longer valid"):
        _rotate(token)


# --- revoke -------------------------------------------------------------------


def test_revoke_refresh_token_revokes_the_family_like_the_sync_helper(fake_redis):
    first = asyncio.run(issue_refresh_token_async(7))
    family = jwt.decode(first, options={"verify_signature": False})["family"]
    second = asyncio.run(issue_refresh_token_async(7, family=family))

    assert asyncio.run(revoke_refresh_token_async(first)) is True

    assert fake_redis.exists(f"refresh_active:{_jti(first)}", f"refresh_active:{_jti(second)}") == 0
    assert fake_redis.exists(f"refresh_family:{family}") == 0
    # Idempotent, and the sync helper agrees.
    assert revoke_refresh_token(first) is True


def test_revoking_an_expired_token_still_revokes_its_family(fake_redis):
    # verify_exp is off: an expired refresh token can still be signed out.
    with freeze_time("2026-01-01 12:00:00") as clock:
        token = asyncio.run(issue_refresh_token_async(7))
        clock.tick(timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS + 1))
        assert asyncio.run(revoke_refresh_token_async(token)) is True


@pytest.mark.parametrize("token", ["garbage", "a.b.c"])
def test_revoking_something_that_is_not_a_token_is_a_no_op(token):
    assert asyncio.run(revoke_refresh_token_async(token)) is True


def test_revoking_a_signed_token_without_a_family_is_a_no_op():
    token = jwt.encode(
        {"sub": "1", "type": "refresh", "exp": int(time.time()) + 60},
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    assert asyncio.run(revoke_refresh_token_async(token)) is True


def test_revoke_reports_false_when_redis_cannot_be_told(unreachable):
    token = create_refresh_token(7)

    assert asyncio.run(revoke_refresh_token_async(token)) is False


# --- blacklist / watermark ----------------------------------------------------


def test_blacklist_token_async_uses_the_key_the_sync_helper_and_auth_read(fake_redis):
    token = create_access_token(7)

    assert asyncio.run(blacklist_token_async(token, 600)) is True

    key = f"blacklist:jti:{_jti(token)}"
    assert fake_redis.get(key) == "1"
    assert 0 < fake_redis.ttl(key) <= 600
    assert asyncio.run(redis_async.is_token_blacklisted(f"jti:{_jti(token)}")) is True


def test_a_token_without_jti_is_known_by_the_token_with_padding_stripped(fake_redis):
    token = jwt.encode(
        {"sub": "7", "exp": int(time.time()) + 60},
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )

    assert asyncio.run(blacklist_token_async(token, 60)) is True

    assert fake_redis.exists(f"blacklist:{token.rstrip('=')}") == 1
    assert blacklist_token(token, 60) is True


def test_a_token_that_does_not_verify_is_not_blacklisted_by_the_async_helper(fake_redis):
    assert asyncio.run(blacklist_token_async("not.a.token", 60)) is False
    assert fake_redis.keys("blacklist:*") == []


def test_blacklist_reports_false_when_redis_cannot_be_told(unreachable):
    assert asyncio.run(blacklist_token_async(create_access_token(7), 60)) is False


def test_watermark_is_the_current_utc_epoch_and_the_same_key_as_the_sync_helper(fake_redis):
    before = time.time()
    assert asyncio.run(invalidate_tokens_issued_before_now_async(42)) is True
    after = time.time()

    watermark = float(fake_redis.get("pwd_changed:42"))
    assert before <= watermark <= after
    assert 0 < fake_redis.ttl("pwd_changed:42") <= settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    # The sync helper overwrites the same key.
    assert invalidate_tokens_issued_before_now(42) is True
    assert float(fake_redis.get("pwd_changed:42")) >= watermark


def test_watermark_reports_false_when_redis_cannot_be_told(unreachable):
    assert asyncio.run(invalidate_tokens_issued_before_now_async(42)) is False


# --- logout -------------------------------------------------------------------


def test_logout_blacklists_the_token_for_its_remaining_lifetime(fake_redis):
    token = create_access_token(7)

    asyncio.run(logout_async(token))

    ttl = fake_redis.ttl(f"blacklist:jti:{_jti(token)}")
    expected = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert expected - 60 <= ttl <= expected + 5


def test_logout_with_a_refresh_token_revokes_only_that_family(fake_redis):
    access = create_access_token(7)
    ended = asyncio.run(issue_refresh_token_async(7))
    other = asyncio.run(issue_refresh_token_async(7))

    asyncio.run(logout_async(access, ended))

    assert fake_redis.exists(f"refresh_active:{_jti(ended)}") == 0
    assert fake_redis.exists(f"refresh_active:{_jti(other)}") == 1
    assert fake_redis.exists("pwd_changed:7") == 0


def test_logout_without_a_refresh_token_signs_out_everywhere(fake_redis):
    access = create_access_token(7)
    first = asyncio.run(issue_refresh_token_async(7))
    second = asyncio.run(issue_refresh_token_async(7))
    bystander = asyncio.run(issue_refresh_token_async(8))

    asyncio.run(logout_async(access))

    assert fake_redis.exists(f"refresh_active:{_jti(first)}", f"refresh_active:{_jti(second)}") == 0
    assert fake_redis.exists(f"refresh_active:{_jti(bystander)}") == 1
    assert fake_redis.exists("pwd_changed:7") == 1


def test_logout_with_a_garbage_refresh_token_still_logs_out(fake_redis):
    access = create_access_token(7)

    asyncio.run(logout_async(access, "garbage"))

    assert fake_redis.exists(f"blacklist:jti:{_jti(access)}") == 1


def test_an_access_token_that_expired_on_the_way_is_still_blacklisted(fake_redis):
    with freeze_time("2026-01-01 12:00:00") as clock:
        token = create_access_token(1)
        clock.tick(timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES, seconds=1))
        asyncio.run(logout_async(token, "garbage"))

        key = f"blacklist:jti:{_jti(token)}"
        assert fake_redis.get(key) == "1"
        assert 0 < fake_redis.ttl(key) <= 1


def test_logout_with_a_token_that_does_not_verify_raises(fake_redis):
    with pytest.raises(RevocationUnavailable, match="access token not blacklisted"):
        asyncio.run(logout_async("not.a.token"))


def test_logout_raises_when_redis_cannot_be_told(unreachable):
    with pytest.raises(RevocationUnavailable):
        asyncio.run(logout_async(create_access_token(7)))
    with pytest.raises(RevocationUnavailable):
        asyncio.run(logout_async(create_access_token(7), create_refresh_token(7)))


def test_logout_raises_at_each_step_that_redis_cannot_record(monkeypatch):
    access = create_access_token(7)

    async def fail(*args, **kwargs):
        return False

    with monkeypatch.context() as patched:
        patched.setattr(security.async_redis_service, "revoke_user_refresh_families", fail)
        with pytest.raises(RevocationUnavailable, match="families not revoked"):
            asyncio.run(logout_async(access))

    with monkeypatch.context() as patched:
        patched.setattr(security, "invalidate_tokens_issued_before_now_async", fail)
        with pytest.raises(RevocationUnavailable, match="not invalidated"):
            asyncio.run(logout_async(access))

    with monkeypatch.context() as patched:
        patched.setattr(security, "revoke_refresh_token_async", fail)
        with pytest.raises(RevocationUnavailable, match="family not revoked"):
            asyncio.run(logout_async(access, "anything"))


def test_logout_of_a_token_with_an_unreadable_subject_stops_after_the_blacklist(fake_redis):
    access = jwt.encode(
        {"sub": "not-a-number", "exp": int(time.time()) + 600, "jti": "j-1"},
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )

    asyncio.run(logout_async(access))

    assert fake_redis.exists("blacklist:jti:j-1") == 1
    assert fake_redis.keys("pwd_changed:*") == []


def test_sync_logout_and_async_logout_leave_the_same_keys(fake_redis):
    access = create_access_token(7)
    other_access = create_access_token(7)
    issue_refresh_token(7)

    logout(access)
    sync_keys = {k.replace(_jti(access), "J") for k in fake_redis.keys("*") if not k.startswith("refresh_active")}
    fake_redis.flushall()
    issue_refresh_token(7)
    asyncio.run(logout_async(other_access))
    async_keys = {
        k.replace(_jti(other_access), "J") for k in fake_redis.keys("*") if not k.startswith("refresh_active")
    }

    assert sync_keys == async_keys
