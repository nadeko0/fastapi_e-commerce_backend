"""Password hashing with bcrypt: the sync calls for Celery and scripts, and
the async wrappers for route handlers, which run bcrypt in a worker thread
behind a per-loop semaphore with a bounded wait (503 when no slot frees).
Split out of app/core/security.py, which re-exports the public names."""

import asyncio
import logging
import os
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import bcrypt
from fastapi import status
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import Problem, ProblemType
from app.core.passwords import normalize_password

logger = logging.getLogger(__name__)


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

def _checkpw(password_text: str, hashed_password: str) -> bool:
    password = password_text.encode("utf-8")
    if len(password) > BCRYPT_MAX_PASSWORD_BYTES:
        # Still pay for one bcrypt check (of the first 72 bytes) before
        # answering False, so a too-long password takes as long as any other
        # wrong one and does not reveal the limit.
        bcrypt.checkpw(password[:BCRYPT_MAX_PASSWORD_BYTES], hashed_password.encode("utf-8"))
        return False
    return bcrypt.checkpw(password, hashed_password.encode("utf-8"))

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # New hashes are made from the NFC form of the password (NIST SP 800-63B-4
    # 3.1.1.2). Hashes made before that were made from the input as typed, so
    # when the NFC form does not match, try the input as given. For ASCII
    # (and any already-NFC) input both are the same string and bcrypt runs once.
    normalised = normalize_password(plain_password)
    if _checkpw(normalised, hashed_password):
        return True
    return normalised != plain_password and _checkpw(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(
        normalize_password(password).encode("utf-8"), bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
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
# generator dependencies). Under concurrent login/register/reset-
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

# Retry-After of the 503 for a request that waited too long for a bcrypt
# slot: long enough for a queue of BCRYPT_WAIT_TIMEOUT_SECONDS to drain.
BCRYPT_BUSY_RETRY_AFTER = 5


@asynccontextmanager
async def _bcrypt_slot() -> AsyncIterator[None]:
    """Hold one bcrypt slot, or answer 503 when none frees up within
    BCRYPT_WAIT_TIMEOUT_SECONDS (load shedding: a quick "not now" to some
    requests instead of a slow answer to all of them)."""
    semaphore = _get_bcrypt_semaphore()
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=settings.BCRYPT_WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "No bcrypt slot within %s s; answered 503", settings.BCRYPT_WAIT_TIMEOUT_SECONDS
        )
        raise Problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            type=ProblemType.SERVICE_OVERLOADED,
            detail="Too many password checks at once, please retry",
            retry_after=BCRYPT_BUSY_RETRY_AFTER,
        ) from None
    try:
        yield
    finally:
        semaphore.release()


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Event-loop-safe wrapper around verify_password for use in async route
    handlers - runs the blocking bcrypt call in a threadpool, bounded by the
    per-loop bcrypt semaphore so unbounded concurrent callers can't all pile
    onto the threadpool (and whatever resources they're holding) at once."""
    async with _bcrypt_slot():
        return await run_in_threadpool(verify_password, plain_password, hashed_password)

async def get_password_hash_async(password: str) -> str:
    """Event-loop-safe wrapper around get_password_hash for use in async
    route handlers - runs the blocking bcrypt call in a threadpool, bounded
    by the per-loop bcrypt semaphore (see verify_password_async)."""
    async with _bcrypt_slot():
        return await run_in_threadpool(get_password_hash, password)
