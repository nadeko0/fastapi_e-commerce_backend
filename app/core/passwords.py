"""Password rules: NFC normalisation and the blocklist.

NIST SP 800-63B-4 section 3.1.1.2: no composition rules, Unicode accepted,
the verifier SHOULD apply NFC normalisation before hashing, and a
prospective password is compared (whole, not by substrings) against a
blocklist of commonly used, expected or compromised passwords, plus
context-specific words such as the service name and the user name. The
rejection must carry its reason.

The list is SecLists 10k-most-common (MIT), see
data/common_passwords.LICENSE.txt. No network lookup is made.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from app.core.config import settings

_BLOCKLIST_FILE = Path(__file__).parent / "data" / "common_passwords.txt"

# A context word shorter than this would match inside too many passwords.
_MIN_CONTEXT_WORD = 4

BLOCKED_MESSAGE = (
    "Password is too common or easy to guess, or contains your email name or "
    "the service name; choose a different one"
)


def normalize_password(password: str) -> str:
    """NFC (the form NIST names), applied before hashing and verifying."""
    return unicodedata.normalize("NFC", password)


def _fold(value: str) -> str:
    return normalize_password(value).casefold()


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    lines = _BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(_fold(line.strip()) for line in lines if line.strip())


def _context_words(email: str | None) -> list[str]:
    words = [re.sub(r"[\W_]+", "", _fold(settings.PROJECT_NAME))]
    if email:
        words.append(_fold(email.rsplit("@", 1)[0]))
    return [w for w in words if len(w) >= _MIN_CONTEXT_WORD]


def is_blocked_password(password: str, email: str | None = None) -> bool:
    """True when the password is on the blocklist (whole password, case
    insensitive) or contains the email's local part or the service name."""
    folded = _fold(password)
    if folded in _blocklist():
        return True
    squeezed = re.sub(r"[\W_]+", "", folded)
    return any(word in folded or word in squeezed for word in _context_words(email))
