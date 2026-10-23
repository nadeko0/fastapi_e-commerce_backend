"""
Request fingerprint for the Idempotency-Key endpoints.

draft-ietf-httpapi-idempotency-key-header-08, section 2.4: "An idempotency
fingerprint MAY be used in conjunction with an idempotency key to determine
the uniqueness of a request. Such a fingerprint is generated from request
payload data by the resource." Section 2.7: a key reused with a different
request payload SHOULD be answered with 422.

The fingerprint is stored next to the key (orders.idempotency_fingerprint,
payments.idempotency_fingerprint). A row stored before the column existed has
none (NULL) and is replayed as before: there is nothing to compare it with.
"""
import hashlib
import json
from typing import Any

from fastapi import HTTPException, status

REUSED_KEY_DETAIL = "Idempotency-Key was already used with a different request payload"


def request_fingerprint(**parts: Any) -> str:
    """
    SHA-256 (hex) of the canonical JSON of the parts that define a request:
    keys sorted, no insignificant whitespace, ASCII only, so equal values
    always give the same bytes. Pass the validated values (a pydantic model's
    `model_dump(mode="json")`, the typed query parameters), not the raw body,
    so that 19.9 and 19.90 are one payload.
    """
    canonical = json.dumps(
        parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def ensure_same_request(stored: str | None, fingerprint: str) -> None:
    """422 when the key was first used by a different request; no-op for a
    row stored without a fingerprint."""
    if stored is not None and stored != fingerprint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=REUSED_KEY_DETAIL,
        )
