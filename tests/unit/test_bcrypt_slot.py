"""The wait for a bcrypt slot is bounded (app/core/password_hashing.py _bcrypt_slot):
a request that finds every slot taken for BCRYPT_WAIT_TIMEOUT_SECONDS is
answered 503 with Retry-After instead of queueing without end."""

import asyncio
import logging

import pytest

from app.core import password_hashing as security
from app.core.config import settings
from app.core.errors import Problem, ProblemType


@pytest.fixture
def one_slot(monkeypatch):
    monkeypatch.setattr(settings, "BCRYPT_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "BCRYPT_WAIT_TIMEOUT_SECONDS", 0.05)


def test_no_slot_in_time_is_a_503_with_retry_after(one_slot, caplog):
    async def scenario():
        taken = security._get_bcrypt_semaphore()
        await taken.acquire()
        try:
            await security.get_password_hash_async("Str0ngPass1")
        finally:
            taken.release()

    with caplog.at_level(logging.WARNING, logger="app.core.password_hashing"), pytest.raises(Problem) as raised:
        asyncio.run(scenario())

    assert raised.value.status_code == 503
    assert raised.value.problem_type == ProblemType.SERVICE_OVERLOADED
    assert raised.value.headers["Retry-After"] == str(security.BCRYPT_BUSY_RETRY_AFTER)
    assert "No bcrypt slot" in caplog.text


def test_verification_waits_for_the_slot_the_same_way(one_slot):
    async def scenario():
        taken = security._get_bcrypt_semaphore()
        await taken.acquire()
        try:
            await security.verify_password_async("Str0ngPass1", security.get_password_hash("Str0ngPass1"))
        finally:
            taken.release()

    with pytest.raises(Problem) as raised:
        asyncio.run(scenario())
    assert raised.value.status_code == 503


def test_a_timed_out_wait_does_not_keep_a_slot(one_slot):
    async def scenario():
        taken = security._get_bcrypt_semaphore()
        await taken.acquire()
        with pytest.raises(Problem):
            await security.get_password_hash_async("Str0ngPass1")
        taken.release()
        # One slot: two hashes in a row only pass if the timed-out wait
        # left nothing behind and each call gave its slot back.
        first = await security.get_password_hash_async("Str0ngPass1")
        second = await security.get_password_hash_async("Str0ngPass1")
        return first, second

    first, second = asyncio.run(scenario())
    assert security.verify_password("Str0ngPass1", first)
    assert security.verify_password("Str0ngPass1", second)


def test_a_free_slot_is_used_without_waiting(one_slot):
    hashed = asyncio.run(security.get_password_hash_async("Str0ngPass1"))
    assert asyncio.run(security.verify_password_async("Str0ngPass1", hashed)) is True
