"""Token invalidation must use real UTC epoch seconds.

logout() and invalidate_tokens_issued_before_now() used
datetime.utcnow().timestamp(), which interprets the naive UTC value as local
time and so is wrong by the host's UTC offset everywhere except in UTC. The
existing tests froze time and never noticed; these compare against the real
clock instead. On a host whose timezone is UTC the two time-based tests
cannot tell the old code from the new, which is what the source check at the
bottom is for.
"""
import asyncio
import inspect
import time

import jwt

from app.core import security
from app.core.security import (
    create_access_token,
    invalidate_tokens_issued_before_now,
    logout,
)


def test_password_change_watermark_is_the_current_utc_epoch(fake_redis):
    before = time.time()
    asyncio.run(invalidate_tokens_issued_before_now(42))
    after = time.time()

    watermark = float(fake_redis.get("pwd_changed:42"))
    assert before <= watermark <= after, (
        f"watermark {watermark} is {before - watermark} s away from now: "
        "tokens issued in that gap before a password reset would stay valid"
    )


def test_logout_blacklists_the_token_for_its_remaining_lifetime(fake_redis):
    token = create_access_token(7)

    asyncio.run(logout(token))

    jti = jwt.decode(token, options={"verify_signature": False})["jti"]
    ttl = fake_redis.ttl(f"blacklist:jti:{jti}")
    expected = security.settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    # Within a minute of the token's full lifetime: not 1 s (the clamp the
    # old code hit west of UTC) and not hours longer (east of it).
    assert expected - 60 <= ttl <= expected + 5, f"blacklist TTL is {ttl} s, token lives {expected} s"


def test_security_module_does_not_take_timestamps_from_naive_utc_datetimes():
    source = inspect.getsource(security)
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert "utcnow().timestamp()" not in code
