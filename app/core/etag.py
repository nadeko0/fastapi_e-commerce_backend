"""Entity tags and conditional requests (RFC 9110 sections 8.8.3, 13.1.1,
13.1.2, 13.2.2).

The tag is a strong validator: SHA-256 of the exact JSON of the response
model (the representation the client sees), so it changes whenever any shown
field changes (including category_name) and is identical whether the
representation was built from the database or from the Redis copy.
"""

import hashlib
import re
from typing import Annotated

from fastapi import Header, HTTPException, status
from pydantic import BaseModel

# Optional request headers; declared here so route signatures stay one line.
IfMatch = Annotated[str | None, Header(description="Entity tag(s) the current resource must match, or *")]
IfNoneMatch = Annotated[str | None, Header(description="Entity tag(s) the client already holds, or *")]

_TAG = re.compile(r'(W/)?"([^"]*)"|\*')


def etag_for(model: BaseModel) -> str:
    digest = hashlib.sha256(model.model_dump_json().encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def _parse(header: str) -> list[tuple[bool, str | None]]:
    """(is_weak, opaque_tag) pairs; a bare '*' is (False, None)."""
    return [(bool(m.group(1)), m.group(2)) for m in _TAG.finditer(header)]


def if_match_passes(header: str | None, current: str) -> bool:
    """RFC 9110 13.1.1: absent header passes; '*' passes (the resource
    exists); otherwise strong comparison, so a weak tag never matches."""
    if header is None:
        return True
    opaque = current.strip('"')
    return any(tag is None or (not weak and tag == opaque) for weak, tag in _parse(header))


def if_none_match_hits(header: str | None, current: str) -> bool:
    """RFC 9110 13.1.2: True when the client's copy is current (GET answers
    304). Weak comparison: the W/ prefix is ignored."""
    if header is None:
        return False
    opaque = current.strip('"')
    return any(tag is None or tag == opaque for _, tag in _parse(header))


def require_if_match(header: str | None, current: str) -> None:
    """412 (RFC 9110 15.5.13) when If-Match is present and false. The
    current tag is returned so the client can refetch deliberately."""
    if not if_match_passes(header, current):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Precondition failed: the resource has changed (If-Match does not match the current ETag)",
            headers={"ETag": current},
        )
