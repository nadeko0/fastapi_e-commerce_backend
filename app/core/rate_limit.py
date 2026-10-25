"""Rate limiting, all of it, in one file.

What it is for: fairness and abuse. One caller must not crowd out the rest,
walk the catalog at machine speed, or guess passwords. Protecting the server
from load as such is a different job, done by the cap on requests in flight
(app/core/overload.py), which needs no Redis.

How it works, top to bottom:

1. LIMITS. Each API operation (method + route template) has a limit written
   the usual way, "300/minute". A handful of routes are named in
   _ROUTE_LIMITS; every other route gets RATE_LIMIT_DEFAULT.
2. WHO. A request with a valid access token is counted per user id, any
   other request per client address (client_address() below, which knows
   about reverse proxies). Auth routes are always counted per address.
3. COUNTING. A fixed window: one Redis counter per (operation, caller) that
   expires when its window ends. Checking and counting are one small Lua
   script, so one atomic round trip per request, whatever the load on a key.
4. OVER THE LIMIT: 429 with Retry-After = seconds until the window ends.
   Every counted response, 429 included, also carries RateLimit-Policy and
   RateLimit (IETF httpapi ratelimit-headers draft), built from the
   counter and TTL the script returned.

Wired in once, in app/main.py, as an application-wide FastAPI dependency
(`rate_limit`). A dependency rather than a middleware because it runs after
routing: FastAPI has already matched the route, so its template is simply
`request.scope["route"].path` and /products/1 and /products/2 are one
operation with one budget. Requests that match no route never get here and
are not counted; neither are the docs and OpenAPI pages, which FastAPI
serves without dependencies.

Known trade-offs, chosen for simplicity:
- A fixed window lets a caller spend one window's budget at its very end
  and the next one's right after, so up to 2 x N in a short span. Smoothing
  that is a job for the proxy in front (nginx limit_req), not for more code
  here.
- If Redis cannot be reached the request is let through (fail open): a
  limiter that cannot count must not take the shop down with it. The one
  exception is the failed-login count (section 5), which fails closed:
  login answers 503 until it can count again.

Why this and not a library, and what real services publish:
scripts/loadtest/results/rate-limiting-research.md, "What was implemented".
"""

import hashlib
import ipaddress
import logging
import math
import re
import unicodedata
from functools import lru_cache
from typing import NamedTuple

import jwt
from fastapi import Request, Response
from redis.exceptions import RedisError
from starlette.status import HTTP_429_TOO_MANY_REQUESTS, HTTP_503_SERVICE_UNAVAILABLE

from app.core.config import RATE_LIMIT_PATTERN, settings
from app.core.errors import Problem, ProblemType
from app.core.probes import PROBE_PATHS
from app.core.security import ACCESS_TOKEN_REQUIRED_CLAIMS, user_id_from_subject
from app.services.redis_async import get_async_redis

logger = logging.getLogger(__name__)


# --- 1. Limits ------------------------------------------------------------

# (method, route template) -> the setting holding its limit. Templates are
# as the routers declare them, without the /api/v1 they are mounted under.
# A route that is not listed gets RATE_LIMIT_DEFAULT; nothing has to be
# registered here for a new route to be limited.
_ROUTE_LIMITS = {
    ("GET", "/products/search"): "RATE_LIMIT_SEARCH",
    ("POST", "/users/register"): "RATE_LIMIT_AUTH",
    ("POST", "/users/login"): "RATE_LIMIT_AUTH",
    ("POST", "/users/password/reset"): "RATE_LIMIT_AUTH",
    ("POST", "/users/password/reset/confirm"): "RATE_LIMIT_AUTH",
    ("POST", "/users/verify-email"): "RATE_LIMIT_AUTH",
    ("POST", "/users/password/reset-token/validate"): "RATE_LIMIT_AUTH",
    ("POST", "/users/verify-email/resend"): "RATE_LIMIT_AUTH",
    ("POST", "/orders"): "RATE_LIMIT_CHECKOUT",
    ("POST", "/orders/{order_id}/pay"): "RATE_LIMIT_CHECKOUT",
    ("POST", "/orders/{order_id}/checkout-session"): "RATE_LIMIT_CHECKOUT",
}

# Never limited. The webhook is authenticated by Stripe's signature and
# arrives in bursts from a few addresses; a health probe answered 429 would
# get the container restarted. The probes answer HEAD too (app/core/probes.py).
_EXEMPT = frozenset(
    {("POST", "/orders/webhooks/stripe")}
    | {(method, path) for method in ("GET", "HEAD") for path in PROBE_PATHS}
)

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


class Rate(NamedTuple):
    count: int    # this many requests...
    seconds: int  # ...per window of this length


@lru_cache(maxsize=64)
def parse_rate(text: str) -> Rate:
    """"300/minute" -> Rate(300, 60); "10/15 minutes" -> Rate(10, 900).
    The settings are checked against the same pattern at startup."""
    match = re.fullmatch(RATE_LIMIT_PATTERN, text)
    if match is None:
        raise ValueError(f"not a rate limit: {text!r} (expected e.g. '300/minute')")
    count, multiple, unit = match.groups()
    return Rate(int(count), int(multiple or 1) * _UNIT_SECONDS[unit])


# --- 2. Who is calling ----------------------------------------------------

def _ip(text: str):
    """The address in `text` as an ipaddress object; None if it is not one."""
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    # "::ffff:1.2.3.4" is how a dual-stack socket reports the IPv4 client.
    return getattr(address, "ipv4_mapped", None) or address


@lru_cache(maxsize=8)
def _trusted_networks(entries: tuple) -> tuple:
    networks = []
    for entry in entries:
        try:
            # strict=False: "10.0.0.5/8" means the network it lies in.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(f"TRUSTED_PROXIES entry {entry!r} is not an address or network; ignored")
    return tuple(networks)


def client_address(request: Request) -> str:
    """The address of the client, for use as a counter key. Never raises.

    Behind a reverse proxy the socket peer is the proxy, and the client is
    only known from X-Forwarded-For - a header anyone can send. So it is
    believed only when the peer is one of settings.TRUSTED_PROXIES, and it
    is read from the right: each proxy appends the address it received the
    request from, so the first entry from the right that is not one of our
    proxies is the client as our outermost proxy saw it. Everything to the
    left of that is whatever the client chose to write.
    """
    peer = request.client.host if request.client else "unknown"
    client = _ip(peer)
    if client is None:
        return peer  # a unix socket path, or the test client's name
    trusted = _trusted_networks(tuple(settings.TRUSTED_PROXIES))

    def is_proxy(address) -> bool:
        return any(address in network for network in trusted)

    if is_proxy(client):
        hops = [
            hop
            for value in request.headers.getlist("x-forwarded-for")
            for hop in value.split(",")
        ]
        for hop in reversed(hops):
            address = _ip(hop)
            if address is None:
                break  # garbage: nothing at or left of it can be believed
            client = address
            if not is_proxy(address):
                break
    if client.version == 6:
        # One subscriber normally holds a whole /64 and can use any address
        # in it, so IPv6 callers are counted per /64, not per address.
        return str(ipaddress.ip_network((client, 64), strict=False))
    return str(client)


def user_id_from_token(request: Request) -> str | None:
    """The user id of a valid access token on this request, or None.

    Signature, expiry and token type only - no blacklist, no database. This
    only chooses which counter the request is charged to; whether it is
    allowed is still decided by the route's own authentication. A token
    that verifies was issued by this application to that user, which is all
    a counter key needs.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        payload = jwt.decode(
            token.strip(),
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ACCESS_TOKEN_REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError:  # malformed, expired, or signed with another key
        return None
    if payload.get("type") != "access":
        return None
    # Only a subject that names a user (see user_id_from_subject): a token
    # whose subject names nobody authenticates nobody, so it must not open a
    # per-user counter either. The address pays for it.
    user_id = user_id_from_subject(payload.get("sub"))
    return None if user_id is None else str(user_id)


# --- 3. Counting ----------------------------------------------------------

# Count one request against KEYS[1], whose window lasts ARGV[1] milliseconds,
# and return {requests in this window including this one, ms until it ends}.
# The window starts with the first request and the key expires with it, so
# there is nothing to clean up and no clock but Redis's own. A script,
# because INCR and PEXPIRE must not be separable: a key that got its INCR
# but never its expiry would count, and block, forever.
_COUNT_LUA = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""

# redis-py's Script object: calls the script by its SHA (EVALSHA) and, when
# the server does not know it yet (first call, Redis restarted), loads it
# and calls again. Made on first use because that needs a client, and the
# client belongs to the running event loop (app/services/redis_async.py);
# every call then names the client it runs on.
_count_script = None


async def _hit(key: str, rate: Rate) -> tuple[int, int]:
    """Count one request against `key`: (requests in this window including
    this one, ms until the window ends). Raises RedisError when Redis
    cannot be asked; what that means is the caller's decision."""
    global _count_script
    redis = get_async_redis()
    if _count_script is None:
        _count_script = redis.register_script(_COUNT_LUA)  # sends nothing yet
    count, ttl_ms = await _count_script(
        keys=[key], args=[rate.seconds * 1000], client=redis
    )
    return int(count), int(ttl_ms)


def _wait(count: int, ttl_ms: int, rate: Rate) -> int:
    """0 if `count` is within `rate`, otherwise the whole seconds until the
    window ends."""
    if count <= rate.count:
        return 0
    return max(1, math.ceil(ttl_ms / 1000))


async def _count(key: str, rate: Rate) -> int:
    """Count one request against `key`. 0 if it is within `rate`, otherwise
    the whole seconds until the window ends. Raises RedisError."""
    count, ttl_ms = await _hit(key, rate)
    return _wait(count, ttl_ms, rate)


def rate_limit_headers(name: str, rate: Rate, count: int, ttl_ms: int) -> dict[str, str]:
    """RateLimit-Policy and RateLimit fields, from the state the counting
    script just returned (no extra round trip). IETF draft-ietf-httpapi-
    ratelimit-headers-11: both are Structured Field Lists whose items are
    the policy name (a String); q = quota, w = window seconds, r = remaining
    quota, t = seconds until the quota is replenished (the window's end)."""
    remaining = max(0, rate.count - count)
    reset = max(0, math.ceil(ttl_ms / 1000))
    return {
        "RateLimit-Policy": f'"{name}";q={rate.count};w={rate.seconds}',
        "RateLimit": f'"{name}";r={remaining};t={reset}',
    }


async def seconds_until_allowed(key: str, rate: Rate) -> int:
    """_count, failing open: 0 as well if Redis cannot be asked."""
    try:
        return await _count(key, rate)
    except RedisError as e:
        # Connection refused, timeout, no free connection in the pool. This
        # except must only ever wrap the Redis call: the 429 is raised by
        # the callers, outside it. (An earlier version raised it inside an
        # `except Exception` and so swallowed its own rejections.)
        logger.error(f"Rate limiting error, request allowed: {e}")
        return 0


def too_many_requests(message: str, retry_after: int) -> Problem:
    return Problem(
        HTTP_429_TOO_MANY_REQUESTS,
        type=ProblemType.RATE_LIMITED,
        detail=message,
        retry_after=retry_after,
    )


# --- 4. The per-request check ---------------------------------------------

class RateLimiter:
    """Holds no state. A class only so that the check has one patchable
    name: the load-test tracer wraps RateLimiter.check_rate_limit."""

    async def check_rate_limit(self, request: Request, response: Response | None = None) -> None:
        """Return if the request may proceed; raise the 429 if not."""
        if not settings.RATE_LIMIT_ENABLED:
            return
        route = request.scope.get("route")
        if route is None:
            return
        operation = (request.method, route.path)
        if operation in _EXEMPT:
            return

        setting = _ROUTE_LIMITS.get(operation, "RATE_LIMIT_DEFAULT")
        user_id = None if setting == "RATE_LIMIT_AUTH" else user_id_from_token(request)
        caller = f"user:{user_id}" if user_id else f"ip:{client_address(request)}"

        rate = parse_rate(getattr(settings, setting))
        try:
            count, ttl_ms = await _hit(f"rl:{operation[0]} {operation[1]}:{caller}", rate)
        except RedisError as e:
            # Fail open, and say nothing about a state that is not known.
            logger.error(f"Rate limiting error, request allowed: {e}")
            return
        headers = rate_limit_headers(
            setting.removeprefix("RATE_LIMIT_").lower(), rate, count, ttl_ms
        )
        retry_after = _wait(count, ttl_ms, rate)
        if retry_after:
            error = too_many_requests("Rate limit exceeded", retry_after)
            error.headers.update(headers)
            raise error
        if response is not None:
            response.headers.update(headers)


limiter = RateLimiter()


async def rate_limit(request: Request, response: Response) -> None:
    """The FastAPI dependency: FastAPI(dependencies=[Depends(rate_limit)]).
    Headers set on `response` are merged into the route's own response."""
    await limiter.check_rate_limit(request, response)


# --- 5. Failed logins, per account ----------------------------------------
#
# RATE_LIMIT_AUTH caps what one address can try on the auth routes, whatever
# emails it names. Login attempts that were not followed by a success are
# also counted per account, twice:
#
# - per (email, client address): RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_
#   ADDRESS ("10/15 minutes"). The hard lock. It stops a guesser at its own
#   address and nobody else: the owner, logging in from anywhere else, is
#   not affected by it.
# - per email, from anywhere: RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL
#   ("100/hour"). The backstop against guessing spread over many addresses,
#   which the first count would give fresh guesses at every address.
#
# Either one used up, login for that email answers 429 until its window
# ends. The per-email count used to be the only one, at 10 per 15 minutes:
# anybody who knew an email could keep its owner out for as long as they
# liked, from one address, by failing on purpose (a completed password
# reset cleared it, and ten more requests set it again). Now one address
# can hold an account's login closed for nobody but itself, and closing it
# for the owner takes failures from many addresses, well past what one
# address may send. That is the trade-off left: an attacker with enough
# addresses can still lock an account; sessions already open keep working.
#
# An attempt is counted when it starts and a successful login deletes the
# counts, so what stays counted is the failures - and a volley of guesses
# sent at the same instant cannot all slip past a check made before any of
# them was counted. An attempt refused by the per-address count is not
# counted against the email: otherwise one address hammering away after
# its own lock would run the email's count up and lock everybody out after
# all. The counts are for the email as submitted, whether or not an
# account exists, and nothing here looks at the database, so the answer
# cannot be used to find out which emails are registered.

def _login_keys(email: str, request: Request) -> tuple[str, str]:
    """(per email and address, per email) counter keys."""
    # Case and surrounding blanks cost a client nothing to vary, so they
    # are folded away. A digest, so that no mailbox sits in a key name.
    normalised = unicodedata.normalize("NFC", email.strip().casefold()).encode("utf-8", "replace")
    email_key = f"rl:login-failures:{hashlib.sha256(normalised).hexdigest()[:32]}"
    return f"{email_key}:{client_address(request)}", email_key


# How long a client is told to wait when the count cannot be kept. A Redis
# outage is rarely over in a second (the 1 of the in-flight cap's 503 in
# app/core/overload.py), and a client retrying every second would only add
# to the load of a recovering service.
LOGIN_UNAVAILABLE_RETRY_AFTER = 30


def login_unavailable() -> Problem:
    """The 503 of a login that cannot be completed because Redis does not
    answer: the failed-attempt count cannot be kept (check_login_attempt), or
    the refresh token cannot be recorded (the route's issue_refresh_token)."""
    return Problem(
        HTTP_503_SERVICE_UNAVAILABLE,
        detail="Login is temporarily unavailable",
        retry_after=LOGIN_UNAVAILABLE_RETRY_AFTER,
    )


async def check_login_attempt(email: str, request: Request) -> None:
    """Call before the password is looked at; raises the 429 when this
    email has used up its failed attempts, from this address or from
    everywhere. At most two Redis round trips, no database.

    Unlike every other limit here this one fails closed: if Redis cannot be
    asked, the attempt is refused with a 503. Failing open would give a
    password guesser unlimited attempts for exactly as long as Redis is
    down - or can be made to look down, by loading it. 503 and not 429:
    the client did nothing wrong and nothing is counted against it; the
    service cannot decide right now, and Retry-After says when to ask
    again. Sessions already open are not affected."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    pair_key, email_key = _login_keys(email, request)
    try:
        retry_after = await _count(
            pair_key, parse_rate(settings.RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS)
        )
        if not retry_after:
            retry_after = await _count(
                email_key, parse_rate(settings.RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL)
            )
    except RedisError as e:
        logger.error(f"Rate limiting error, login refused: {e}")
        raise login_unavailable() from e
    if retry_after:
        raise too_many_requests("Too many failed login attempts", retry_after)


async def clear_login_failures(email: str, request: Request) -> None:
    """Call after a successful login or a completed password reset: clears
    the email's count and its count at this address. Counts other addresses
    ran up for the email stay: they are somebody else's failures."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    try:
        await get_async_redis().delete(*_login_keys(email, request))
    except RedisError as e:
        logger.error(f"Rate limiting error, failed-login count not cleared: {e}")
