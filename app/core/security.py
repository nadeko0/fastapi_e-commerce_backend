import asyncio
import os
import uuid
import weakref
from datetime import datetime, timedelta
from typing import Optional, Tuple, Union

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.models.user import User, UserRole
from app.services.redis import RedisService

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
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

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
        semaphore = asyncio.Semaphore((os.cpu_count() or 1) * 4)
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

def create_access_token(subject: Union[str, int]) -> str:
    now = datetime.utcnow()
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"exp": expire, "iat": now, "sub": str(subject), "type": "access"}
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def create_refresh_token(subject: Union[str, int], jti: Optional[str] = None, family: Optional[str] = None) -> str:
    now = datetime.utcnow()
    expire = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode = {
        "exp": expire,
        "iat": now,
        "sub": str(subject),
        "type": "refresh",
        # jti: identifies this specific token so it can be single-use
        # (see consume_refresh_token). family: shared across every token in
        # one rotation chain, so a replay of an already-rotated token can
        # revoke the whole chain instead of just the one reused token.
        "jti": jti or str(uuid.uuid4()),
        "family": family or str(uuid.uuid4()),
    }
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
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
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    redis_service.register_refresh_token(payload["jti"], user_id, payload["family"], ttl_seconds)
    return token


def rotate_refresh_token(token: str, db: Session) -> Tuple[str, str]:
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
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise RefreshTokenError("Could not validate refresh token")

    if payload.get("type") != "refresh":
        raise RefreshTokenError("Not a refresh token")

    user_id = payload.get("sub")
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

    password_changed_at = redis_service.get(f"pwd_changed:{user_id}")
    if password_changed_at is not None and (issued_at is None or issued_at < password_changed_at):
        raise RefreshTokenError("Refresh token no longer valid")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None or not user.is_active:
        raise RefreshTokenError("User not found or inactive")

    new_access_token = create_access_token(user.id)
    new_refresh_token = issue_refresh_token(user.id, family=family)

    return new_access_token, new_refresh_token


def revoke_refresh_token(token: str) -> None:
    """Best-effort revocation of a refresh token's entire family, used on
    logout so a refresh cannot succeed after sign-out. Silently no-ops on a
    malformed/garbage/expired token - logout should not fail just because
    the client sent a bad refresh token alongside a valid access token."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except JWTError:
        return
    family = payload.get("family")
    if family:
        redis_service.revoke_refresh_family(family)


def logout(access_token: str, refresh_token: Optional[str] = None) -> None:
    """Blacklist the current access token and, if provided, revoke the
    refresh token's whole family."""
    try:
        payload = jwt.decode(access_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        expires_in = max(int(payload["exp"] - datetime.utcnow().timestamp()), 1)
    except JWTError:
        expires_in = 1
    blacklist_token(access_token, expires_in)
    if refresh_token:
        revoke_refresh_token(refresh_token)

# Token fixation mitigation: a token issued before a password reset must not
# remain usable afterward, otherwise a user resetting their password because
# they suspect their current token/password is compromised gains nothing.
# There is no per-token registry (JWTs are stateless and the reset flow has
# no access token to blacklist - the user may not even be logged in when
# requesting a reset), so instead of blacklisting individual tokens we record
# a per-user "password changed at" watermark in Redis and reject any access
# token whose `iat` predates it. TTL matches the longest-lived token
# (refresh) so the watermark outlives every token that could still exist.
def invalidate_tokens_issued_before_now(user_id: Union[str, int]) -> bool:
    key = f"pwd_changed:{user_id}"
    ttl_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    return redis_service.setex(key, ttl_seconds, int(datetime.utcnow().timestamp()))

async def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


    if redis_service.is_blacklisted(token):
        raise credentials_exception

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")
        issued_at = payload.get("iat")

        if user_id is None or token_type != "access":
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    password_changed_at = redis_service.get(f"pwd_changed:{user_id}")
    if password_changed_at is not None and (issued_at is None or issued_at < password_changed_at):
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id)).first()
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

def blacklist_token(token: str, expires_in: int) -> bool:
    return redis_service.add_to_blacklist(token, expires_in)

def validate_token(token: str) -> Optional[dict]:
    try:
        if redis_service.is_blacklisted(token):
            return None

        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except JWTError:
        return None

def generate_password_reset_token(email: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode = {"exp": expire, "sub": email, "type": "reset"}
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def verify_password_reset_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload["type"] != "reset":
            return None
        return payload["sub"]
    except JWTError:
        return None

def generate_email_verification_token(email: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
    to_encode = {"exp": expire, "sub": email, "type": "verify_email"}
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

def verify_email_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload["type"] != "verify_email":
            return None
        return payload["sub"]
    except JWTError:
        return None
