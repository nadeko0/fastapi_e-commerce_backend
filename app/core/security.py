import asyncio
import hashlib
import hmac
import os
import re
import time
import uuid
import weakref
from datetime import timedelta
from typing import NamedTuple, Optional, Tuple, Union

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import settings
from app.core.database import get_async_db
from app.models.user import User, UserRole
from app.services.redis import RedisService
from app.services.redis_async import AsyncRedisService, is_token_blacklisted, is_token_superseded

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/users/login")
redis_service = RedisService()

# passlib (last released 2020, unmaintained) is incompatible with bcrypt>=5:
# hashing raises "password cannot be longer than 72 bytes" even for short
# passwords because passlib's version-detection shim breaks. Call bcrypt
# directly instead.
#
# bcrypt.checkpw/hashpw are CPU-bound and block for the full duration of the
# call (~200-300ms+ at BCRYPT_ROUNDS=12). These sync versions are for
# non-event-loop callers only (Celery tasks in app/tasks.py, one-off scripts,
# sync test fixtures) where there's no event loop to block. Any `async def`
# route handler must use the *_async wrappers below instead, which offload
# the blocking call to a worker thread via run_in_threadpool so one slow
# bcrypt call doesn't stall every other request on the same uvicorn worker.
# bcrypt only uses the first 72 bytes of a password, and since bcrypt 5.0.0
# checkpw/hashpw raise ValueError for anything longer instead of truncating
# (OWASP Password Storage Cheat Sheet: "bcrypt has a maximum length input
# length of 72 bytes"). Registration and password change refuse such
# passwords (422), so no stored password is that long and a longer one can
# never match: it is simply a wrong password.
BCRYPT_MAX_PASSWORD_BYTES = 72

def verify_password(plain_password: str, hashed_password: str) -> bool:
    password = plain_password.encode("utf-8")
    if len(password) > BCRYPT_MAX_PASSWORD_BYTES:
        # Still pay for one bcrypt check (of the first 72 bytes) before
        # answering False, so a too-long password takes as long as any other
        # wrong one and the response time does not reveal the length check.
        bcrypt.checkpw(password[:BCRYPT_MAX_PASSWORD_BYTES], hashed_password.encode("utf-8"))
        return False
    return bcrypt.checkpw(password, hashed_password.encode("utf-8"))

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    ).decode("utf-8")

# Precomputed once at import time so a login attempt for a nonexistent email
# still has a syntactically valid bcrypt hash to check against - passing ""
# or None to bcrypt.checkpw raises ValueError("Invalid salt") rather than
# returning False, which would surface as a 500 instead of a 401.
# Deliberately at settings.BCRYPT_ROUNDS (the real cost factor), not a fixed
# cheap value: the whole point is that checking against this hash costs the
# same as checking against a real one, so login response time can't be used
# to distinguish "no such account" from "wrong password" (a user-enumeration
# side channel). Callers MUST still run the verify call unconditionally
# (never skip it via a short-circuited `user is None or ...` check) - the
# dummy hash only helps if bcrypt actually runs every time.
DUMMY_PASSWORD_HASH = get_password_hash(str(uuid.uuid4()))

# run_in_threadpool dispatches onto anyio's shared default worker pool
# (capacity ~40, shared with every other sync-dependency/threadpool call in
# the process - including FastAPI's own threadpool wrapping of sync
# `Depends(get_db)` generators). Under concurrent login/register/reset-
# password load, unbounded bcrypt calls can all pile into that shared pool
# at once; on a CPU-starved host (bcrypt is CPU-bound, not I/O-bound) they
# queue behind each other for real wall-clock time rather than actually
# running in parallel. Any caller that still holds a resource (e.g. a
# checked-out DB connection) open across this await pays for that entire
# queueing delay, not just one hash's cost. Bounding concurrency to the
# number of CPUs keeps each call's queueing time bounded instead of growing
# with total concurrent request volume. A small multiple of the CPU count
# (rather than an exact 1:1 cap) still prevents unbounded pileup - the
# production failure mode this guards against is hundreds of concurrent
# requests all queuing bcrypt work at once, not a handful of legitimate
# concurrent logins - while leaving enough headroom that a normal moderate
# burst doesn't serialize into needless extra queueing latency.
#
# asyncio.Semaphore binds itself to whichever event loop first awaits it and
# raises RuntimeError from any other loop - fine for a real uvicorn worker
# (one loop for the process's lifetime) but not safe as a plain module-level
# singleton in general (e.g. multiple event loops in one process, as every
# test in this suite creates its own). Keyed lazily by the running loop
# instead of created once at import time, so each loop gets its own
# semaphore; a WeakKeyDictionary lets entries for closed/discarded loops be
# garbage collected instead of accumulating.
_bcrypt_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)

def _get_bcrypt_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _bcrypt_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(
            settings.BCRYPT_MAX_CONCURRENCY or (os.cpu_count() or 1) * 4
        )
        _bcrypt_semaphores[loop] = semaphore
    return semaphore

async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Event-loop-safe wrapper around verify_password for use in async route
    handlers - runs the blocking bcrypt call in a threadpool, bounded by the
    per-loop bcrypt semaphore so unbounded concurrent callers can't all pile
    onto the threadpool (and whatever resources they're holding) at once."""
    async with _get_bcrypt_semaphore():
        return await run_in_threadpool(verify_password, plain_password, hashed_password)

async def get_password_hash_async(password: str) -> str:
    """Event-loop-safe wrapper around get_password_hash for use in async
    route handlers - runs the blocking bcrypt call in a threadpool, bounded
    by the per-loop bcrypt semaphore (see verify_password_async)."""
    async with _get_bcrypt_semaphore():
        return await run_in_threadpool(get_password_hash, password)

# The canonical spelling of a user id, which is all this application ever
# writes into `sub` (str(user.id)): ASCII digits, no sign, no leading zero,
# no blanks.
_USER_ID_SUBJECT = re.compile(r"[1-9][0-9]{0,9}")
# The largest id the users.id column (INTEGER, 32 bits in Postgres) holds.
_MAX_USER_ID = 2**31 - 1

def user_id_from_subject(subject: object) -> Optional[int]:
    """The user id a verified token's `sub` claim names, or None.

    Anything but the canonical spelling names nobody and is refused, not
    interpreted. Converting it with int() raised ValueError on letters (a
    500 rather than a 401), accepted spellings no token of ours has (" 1",
    "01", non-ASCII digits), and passed the database ids its column cannot
    hold. A signature only proves we issued the token; this proves it is
    one of the tokens we issue.
    """
    if not isinstance(subject, str) or not _USER_ID_SUBJECT.fullmatch(subject):
        return None
    user_id = int(subject)
    return user_id if user_id <= _MAX_USER_ID else None

# `iat` is a NumericDate with sub-second precision (RFC 7519 allows a
# fractional one): the epoch seconds as a float, compared against the
# per-user watermark (invalidate_tokens_issued_before_now), which has the
# same precision. A datetime would not do: PyJWT 2.15 turns a datetime claim
# into whole seconds on encode (timegm), and a token issued earlier in the
# same second as a password change or a sign-out-everywhere then survived
# it. A float is encoded as is, and on decode PyJWT checks it with int(),
# so validation is unchanged. `exp` stays whole seconds. Tokens issued
# before this carry a whole-second iat and still compare correctly.
def _issued_at_and_expiry(lifetime: timedelta) -> Tuple[float, int]:
    issued_at = time.time()
    return issued_at, int(issued_at) + int(lifetime.total_seconds())

def create_access_token(subject: Union[str, int]) -> str:
    issued_at, expire = _issued_at_and_expiry(
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    # jti: identifies this token on the logout blacklist (see _blacklist_id).
    to_encode = {
        "exp": expire,
        "iat": issued_at,
        "sub": str(subject),
        "type": "access",
        "jti": str(uuid.uuid4()),
    }
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def create_refresh_token(subject: Union[str, int], jti: Optional[str] = None, family: Optional[str] = None) -> str:
    issued_at, expire = _issued_at_and_expiry(
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    to_encode = {
        "exp": expire,
        "iat": issued_at,
        "sub": str(subject),
        "type": "refresh",
        # jti: identifies this specific token so it can be single-use
        # (see consume_refresh_token). family: shared across every token in
        # one rotation chain, so a replay of an already-rotated token can
        # revoke the whole chain instead of just the one reused token.
        "jti": jti or str(uuid.uuid4()),
        "family": family or str(uuid.uuid4()),
    }
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


class RefreshTokenError(Exception):
    """Raised when a presented refresh token is malformed, expired, of the
    wrong type, already redeemed (replay), or belongs to a user who is no
    longer active."""


def issue_refresh_token(user_id: Union[str, int], family: Optional[str] = None) -> str:
    """Create a refresh token and register its jti with Redis so it can
    later be validated, rotated, and reuse-detected. `family` is passed when
    rotating an existing chain (login omits it, which starts a new chain)."""
    token = create_refresh_token(user_id, family=family)
    payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    redis_service.register_refresh_token(payload["jti"], user_id, payload["family"], ttl_seconds)
    return token


async def rotate_refresh_token(token: str, db: Session) -> Tuple[str, str]:
    """Validate a refresh token and, if valid and unused, rotate it: returns
    a (new_access_token, new_refresh_token) pair. Deliberately does NOT
    require a valid access token - a refresh token's whole purpose is to
    obtain a new access token after the old one has expired.

    Reuse detection: a refresh token is single-use (see
    RedisService.consume_refresh_token). If the same jti is presented a
    second time - which only happens if it was intercepted and already
    redeemed by its legitimate owner, or vice versa - that is treated as a
    stolen-token signal and the entire token family is revoked, forcing
    re-login instead of silently issuing more tokens on a compromised chain.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise RefreshTokenError("Could not validate refresh token")

    if payload.get("type") != "refresh":
        raise RefreshTokenError("Not a refresh token")

    user_id = user_id_from_subject(payload.get("sub"))
    jti = payload.get("jti")
    family = payload.get("family")
    issued_at = payload.get("iat")
    if user_id is None or jti is None or family is None:
        raise RefreshTokenError("Could not validate refresh token")

    consumed = redis_service.consume_refresh_token(jti)
    if consumed is None:
        # Unknown jti (never issued / already expired out of Redis) or a
        # replay of an already-rotated token - burn the rest of the chain
        # either way; if it wasn't tracked this is a harmless no-op.
        redis_service.revoke_refresh_family(family)
        raise RefreshTokenError("Refresh token already used or invalid")

    # The same check, and the same fail-closed answer, as get_current_user.
    # It used to read the watermark through RedisService.get, which answers
    # None on a Redis error: a refresh token issued before a password reset
    # was redeemed again whenever that read failed.
    if await is_token_superseded(user_id, issued_at):
        raise RefreshTokenError("Refresh token no longer valid")

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise RefreshTokenError("User not found or inactive")

    new_access_token = create_access_token(user.id)
    new_refresh_token = issue_refresh_token(user.id, family=family)

    return new_access_token, new_refresh_token


def revoke_refresh_token(token: str) -> bool:
    """Revoke a refresh token's entire family, used on logout so a refresh
    cannot succeed after sign-out. A malformed/garbage/expired token is a
    no-op (True: there is nothing to revoke) - logout should not fail just
    because the client sent a bad refresh token alongside a valid access
    token. False only when Redis could not be told."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return True
    family = payload.get("family")
    if family:
        return redis_service.revoke_refresh_family(family)
    return True


class RevocationUnavailable(Exception):
    """Raised when tokens should have been revoked and Redis, where
    revocation lives, could not be told. The caller must not report
    success: a logout or a password reset that leaves the old tokens
    working is worse than one the user knows to retry."""


def logout(access_token: str, refresh_token: Optional[str] = None) -> None:
    """Blacklist the current access token, and revoke the refresh token's
    whole family - or, without a refresh token, every token of the user
    ("sign out everywhere"): without one there is no telling which session
    is ending, and leaving them all valid let a refresh token go on minting
    access tokens for REFRESH_TOKEN_EXPIRE_DAYS after "logout". Raises
    RevocationUnavailable if any of it could not be recorded."""
    payload = {}
    try:
        payload = jwt.decode(access_token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
        # time.time(), not datetime.utcnow().timestamp(): .timestamp() on a
        # naive datetime reads it as *local* time, so on any host whose
        # timezone is not UTC the result is off by the UTC offset, while the
        # token's `exp` is true UTC. West of UTC that made the remaining
        # lifetime negative, clamped to 1 s below - a logged-out token was
        # blacklisted for one second and then worked again.
        expires_in = max(int(payload["exp"] - time.time()), 1)
    except jwt.PyJWTError:
        expires_in = 1
    # The access token was authenticated by the route a moment ago, so it
    # verifies: False here means Redis was not told.
    if not blacklist_token(access_token, expires_in):
        raise RevocationUnavailable("access token not blacklisted")
    if refresh_token:
        if not revoke_refresh_token(refresh_token):
            raise RevocationUnavailable("refresh token family not revoked")
        return
    user_id = user_id_from_subject(payload.get("sub"))
    if user_id is None:
        return
    if not redis_service.revoke_user_refresh_families(user_id):
        raise RevocationUnavailable("refresh token families not revoked")
    # The per-user family index only knows the families registered since it
    # was added: a refresh token issued before that is in no index and
    # survived the revocation above until it expired. The password-change
    # watermark has no such gap: every access and refresh token of the user
    # issued before now is refused (is_token_superseded), whether or not
    # anything else knows of it.
    if not invalidate_tokens_issued_before_now(user_id):
        raise RevocationUnavailable("tokens issued before the logout not invalidated")

# Token fixation mitigation: a token issued before a password reset must not
# remain usable afterward, otherwise a user resetting their password because
# they suspect their current token/password is compromised gains nothing.
# Signing out everywhere (logout without a refresh token) moves the same
# watermark, for the same reason.
# There is no per-token registry (JWTs are stateless and the reset flow has
# no access token to blacklist - the user may not even be logged in when
# requesting a reset), so instead of blacklisting individual tokens we record
# a per-user "password changed at" watermark in Redis and reject any access
# token whose `iat` predates it. TTL matches the longest-lived token
# (refresh) so the watermark outlives every token that could still exist.
# Returns False when Redis could not be told; the caller must then not
# report the password change (or the logout) as done (see reset_password).
def invalidate_tokens_issued_before_now(user_id: Union[str, int]) -> bool:
    key = f"pwd_changed:{user_id}"
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    # time.time() for the same reason as in logout() above. With
    # datetime.utcnow().timestamp() the watermark was shifted by the host's UTC
    # offset: on a machine at UTC+2 it lay two hours in the past, so every
    # token issued in the two hours before a password reset stayed valid -
    # exactly the tokens this exists to kill. Invisible where TZ is UTC
    # (most containers), which is why it went unnoticed.
    # Not rounded to the second: compared with a token's sub-second iat
    # (see _issued_at_and_expiry), so that a token issued earlier in the
    # same second is refused and one issued right after is not. A watermark
    # stored in whole seconds before this still compares correctly.
    return redis_service.setex(key, ttl_seconds, time.time())

# The three dependencies below run on the event loop for every authenticated
# request, so nothing in them may block: the Redis lookups go through the
# async client (app/services/redis_async.py) and the user is loaded through
# an AsyncSession. They used to call the sync RedisService and a sync
# Session inline, which stopped the worker for the length of each round trip
# and held a pooled sync connection for the whole request on top of the
# route's own.
#
# What callers get back is a detached snapshot of the user row:
# - Every column is loaded and readable (id, email, role, flags, ...).
# - Relationships (current_user.addresses, current_user.orders) are NOT
#   loaded and cannot be: there is no session to load them through, and
#   touching one raises DetachedInstanceError. Query what you need through
#   the route's own session, filtered by current_user.id.
# - Changing an attribute changes this object only. To update the user, load
#   the row in the route's own session (see update_consent in
#   app/api/v1/users.py) and change that one.
async def get_current_user(
    db: AsyncSession = Depends(get_async_db),
    token: str = Depends(oauth2_scheme)
) -> User:

    # A rejected token that was presented is named as such (RFC 6750
    # section 3). oauth2_scheme answers a request with no Authorization
    # header at once; "Authorization: Bearer" with nothing after it gets
    # here with an empty token, which is no token, so the bare challenge.
    challenge = 'Bearer error="invalid_token"' if token else "Bearer"
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": challenge},
    )

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM]
        )
        user_id = user_id_from_subject(payload.get("sub"))
        token_type: str = payload.get("type")
        issued_at = payload.get("iat")

        if user_id is None or token_type != "access":
            raise credentials_exception

    except jwt.PyJWTError:
        raise credentials_exception

    if await is_token_blacklisted(_blacklist_id(token, payload)):
        raise credentials_exception

    if await is_token_superseded(user_id, issued_at):
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is not None:
        # Detach the row before handing it out, so that the object behaves
        # the same in every route: a later change to it is never flushed by
        # this session's commit, whether or not the route shares the session.
        db.expunge(user)
    # End the read transaction now. The session itself lives until the
    # request's dependencies are torn down, and without this it would keep
    # its pooled connection checked out, idle in a transaction, for the
    # whole request - the second-connection-per-request cost this dependency
    # used to have on the sync pool. commit rather than rollback: a route on
    # get_async_db shares this session, and nothing of its own may be thrown
    # away here.
    await db.commit()
    if user is None:
        raise credentials_exception

    return user

async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    return current_user

async def get_current_admin_user(
    current_user: User = Depends(get_current_active_user),
) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges"
        )
    return current_user

# Declares the bearer scheme on a route that also serves callers without a
# token, so that the OpenAPI document (and the "Authorize" button of the
# docs) still attaches a token to it. It decides nothing: with
# auto_error=False it never rejects a request, and its result is not used.
_optional_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/users/login", auto_error=False
)

async def get_optional_active_user(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    _declared: Optional[str] = Depends(_optional_oauth2_scheme),
) -> Optional[User]:
    """For a route that serves both logged-in and anonymous callers (the
    cart): the active user when the request carries an Authorization
    header, None when it carries none.

    The header alone decides which it is. Once it is present, the request
    is authenticated by the very dependencies above, called in the same
    order with the same arguments, so every way they reject a caller
    applies unchanged: a missing, malformed, expired, blacklisted or
    superseded token is a 401 and an inactive user a 400. It is never
    quietly treated as "nobody": a client whose token has just expired must
    be told so, not be shown some other, empty cart as if its own had
    vanished.

    The user returned is the same detached snapshot get_current_user
    documents.
    """
    if "authorization" not in request.headers:
        return None
    token = await oauth2_scheme(request)
    current_user = await get_current_user(db=db, token=token)
    return await get_current_active_user(current_user=current_user)

def _blacklist_id(token: str, payload: dict) -> str:
    """What the logout blacklist knows a verified access token by.

    Not the token string: one token has more than one spelling (PyJWT also
    accepts the signature segment with its base64url "=" padding), and a
    blacklist of strings let a logged-out token back in with "=" appended.
    The jti is the same in every spelling.

    Access tokens issued before they carried a jti stay valid for
    ACCESS_TOKEN_EXPIRE_MINUTES after the deploy. They are known by the
    token with that padding stripped: the one spelling PyJWT produces, and
    the key a logout before the deploy already wrote, so those entries
    still hold. This branch can go once no such token can be unexpired.
    """
    jti = payload.get("jti")
    if jti:
        return f"jti:{jti}"
    return token.rstrip("=")

def blacklist_token(token: str, expires_in: int) -> bool:
    """Revoke an access token for `expires_in` seconds. A token that does
    not verify is not stored: nothing would accept it anyway."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return False
    return redis_service.add_to_blacklist(_blacklist_id(token, payload), expires_in)


# -- Async twins of the token helpers --------------------------------------
#
# The same behaviour and the same Redis keys as issue_refresh_token,
# rotate_refresh_token, revoke_refresh_token, logout,
# invalidate_tokens_issued_before_now and blacklist_token above, for
# coroutines: Redis is reached through AsyncRedisService (never blocking the
# loop) and rotate_refresh_token_async takes an AsyncSession. Failure
# semantics are unchanged and fail closed: a Redis error while recording a
# revocation is reported (False / RevocationUnavailable), never swallowed.
# The sync versions stay until the last route that calls them is migrated.

async_redis_service = AsyncRedisService()


async def issue_refresh_token_async(user_id: Union[str, int], family: Optional[str] = None) -> str:
    """Async issue_refresh_token."""
    token = create_refresh_token(user_id, family=family)
    payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    await async_redis_service.register_refresh_token(payload["jti"], user_id, payload["family"], ttl_seconds)
    return token


async def rotate_refresh_token_async(token: str, db: AsyncSession) -> Tuple[str, str]:
    """Async rotate_refresh_token (see it for the reuse-detection rules):
    Redis through the async client, the user loaded with select() on an
    AsyncSession."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise RefreshTokenError("Could not validate refresh token")

    if payload.get("type") != "refresh":
        raise RefreshTokenError("Not a refresh token")

    user_id = user_id_from_subject(payload.get("sub"))
    jti = payload.get("jti")
    family = payload.get("family")
    issued_at = payload.get("iat")
    if user_id is None or jti is None or family is None:
        raise RefreshTokenError("Could not validate refresh token")

    consumed = await async_redis_service.consume_refresh_token(jti)
    if consumed is None:
        # Unknown jti or a replay of an already-rotated token: burn the rest
        # of the chain either way; if it was not tracked this is a no-op.
        await async_redis_service.revoke_refresh_family(family)
        raise RefreshTokenError("Refresh token already used or invalid")

    if await is_token_superseded(user_id, issued_at):
        raise RefreshTokenError("Refresh token no longer valid")

    user = (await db.execute(select(User).where(User.id == user_id).limit(1))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise RefreshTokenError("User not found or inactive")

    new_access_token = create_access_token(user.id)
    new_refresh_token = await issue_refresh_token_async(user.id, family=family)

    return new_access_token, new_refresh_token


async def revoke_refresh_token_async(token: str) -> bool:
    """Async revoke_refresh_token: a token that does not decode is a no-op
    (True); False only when Redis could not be told."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return True
    family = payload.get("family")
    if family:
        return await async_redis_service.revoke_refresh_family(family)
    return True


async def blacklist_token_async(token: str, expires_in: int) -> bool:
    """Async blacklist_token."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.PyJWTError:
        return False
    return await async_redis_service.add_to_blacklist(_blacklist_id(token, payload), expires_in)


async def invalidate_tokens_issued_before_now_async(user_id: Union[str, int]) -> bool:
    """Async invalidate_tokens_issued_before_now (same key, same
    sub-second watermark). False when Redis could not be told."""
    key = f"pwd_changed:{user_id}"
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    return await async_redis_service.setex(key, ttl_seconds, time.time())


async def logout_async(access_token: str, refresh_token: Optional[str] = None) -> None:
    """Async logout (see it for the rules). Raises RevocationUnavailable if
    any of it could not be recorded."""
    payload = {}
    try:
        payload = jwt.decode(access_token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
        expires_in = max(int(payload["exp"] - time.time()), 1)
    except jwt.PyJWTError:
        expires_in = 1
    if not await blacklist_token_async(access_token, expires_in):
        raise RevocationUnavailable("access token not blacklisted")
    if refresh_token:
        if not await revoke_refresh_token_async(refresh_token):
            raise RevocationUnavailable("refresh token family not revoked")
        return
    user_id = user_id_from_subject(payload.get("sub"))
    if user_id is None:
        return
    if not await async_redis_service.revoke_user_refresh_families(user_id):
        raise RevocationUnavailable("refresh token families not revoked")
    if not await invalidate_tokens_issued_before_now_async(user_id):
        raise RevocationUnavailable("tokens issued before the logout not invalidated")

# A password reset token is single-use without any server-side state: it
# carries a fingerprint of the password hash it was issued against, and is
# only good while the account still has that hash. Using it changes the hash,
# so the token is spent; any other password change spends it too, and so do
# all the older reset links still lying in the mailbox. A Redis "used"
# marker would do the first but not the other two, and would make resets
# depend on Redis being up. The fingerprint is an HMAC rather than a slice
# of the hash itself because a JWT's payload is readable by whoever holds
# the link, and the hash is nobody's business.
#
# Tokens issued before this carried no fingerprint and are refused: they
# are reset links, a new one is one click away.
def _password_fingerprint(hashed_password: str) -> str:
    return hmac.new(
        settings.JWT_SECRET_KEY.get_secret_value().encode("utf-8"),
        hashed_password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class PasswordResetClaims(NamedTuple):
    email: str
    password_fingerprint: str

    def matches(self, hashed_password: str) -> bool:
        """Whether the token was issued against this (the current) hash."""
        return hmac.compare_digest(
            self.password_fingerprint, _password_fingerprint(hashed_password)
        )


def generate_password_reset_token(email: str, hashed_password: str) -> str:
    """`hashed_password` is the account's current hash: the token is good
    until it changes (see PasswordResetClaims.matches)."""
    expire = utc_now() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    to_encode = {
        "exp": expire,
        "sub": email,
        "type": "reset",
        "pwd": _password_fingerprint(hashed_password),
    }
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def verify_password_reset_token(token: str) -> Optional[PasswordResetClaims]:
    """The claims of a well-formed, unexpired reset token, or None. Whether
    it is still good for the account is PasswordResetClaims.matches, which
    needs the account's current hash."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    email = payload.get("sub")
    fingerprint = payload.get("pwd")
    if payload.get("type") != "reset" or not isinstance(email, str) or not isinstance(fingerprint, str):
        return None
    return PasswordResetClaims(email, fingerprint)

def generate_email_verification_token(email: str) -> str:
    expire = utc_now() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
    to_encode = {"exp": expire, "sub": email, "type": "verify_email"}
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def verify_email_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(), algorithms=[settings.JWT_ALGORITHM])
        if payload["type"] != "verify_email":
            return None
        return payload["sub"]
    except jwt.PyJWTError:
        return None
