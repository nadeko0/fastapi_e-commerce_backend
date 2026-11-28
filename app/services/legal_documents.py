from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from string import Formatter

from app.core.config import settings

_BUILTIN_DIR = Path(__file__).resolve().parents[1] / "legal"
_FORMATTER = Formatter()


@dataclass(frozen=True, slots=True)
class LegalDocumentTemplate:
    """Parsed legal document template loaded from a Markdown file."""

    text: str
    version: str
    effective_date: datetime
    placeholders: tuple[str, ...]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_front_matter(raw_text: str, name: str) -> tuple[str, datetime, str]:
    lines = raw_text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Legal document {name!r} is missing front matter")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError(f"Legal document {name!r} has unterminated front matter")

    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"Invalid front matter line in {name!r}: {line!r}")
        key, raw_val = stripped.split(":", 1)
        meta[key.strip()] = _strip_quotes(raw_val.strip())

    version = meta.get("version", "").strip()
    if not version:
        raise ValueError(f"Legal document {name!r} front matter is missing 'version'")

    effective_raw = meta.get("effective", "").strip()
    if not effective_raw:
        raise ValueError(f"Legal document {name!r} front matter is missing 'effective'")

    parsed_date = date.fromisoformat(effective_raw)
    effective_date = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return version, effective_date, body


def _extract_placeholders(text: str, name: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for _, field_name, _, _ in _FORMATTER.parse(text):
        if field_name is None:
            continue
        if not field_name:
            raise ValueError(f"Empty placeholder {{}} is not allowed in legal document {name!r}")
        seen[field_name] = None
    return tuple(seen)


@lru_cache(maxsize=32)
def _load_cached(directory: Path, name: str) -> LegalDocumentTemplate:
    path = directory / f"{name}.md"
    raw_text = path.read_text(encoding="utf-8")
    version, effective_date, body = _parse_front_matter(raw_text, name)
    placeholders = _extract_placeholders(body, name)
    return LegalDocumentTemplate(
        text=body,
        version=version,
        effective_date=effective_date,
        placeholders=placeholders,
    )


def _resolve_docs_dir() -> Path:
    if settings.LEGAL_DOCS_DIR:
        return Path(settings.LEGAL_DOCS_DIR).resolve()
    return _BUILTIN_DIR


def load_legal_document(
    name: str,
    allowed_placeholders: Iterable[str] | None = None,
) -> LegalDocumentTemplate:
    """Load and cache a legal document by name, validating its placeholders."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid legal document name: {name!r}")

    doc = _load_cached(_resolve_docs_dir(), name)
    if allowed_placeholders is not None:
        allowed = frozenset(allowed_placeholders)
        for placeholder in doc.placeholders:
            if placeholder not in allowed:
                raise ValueError(
                    f"Unknown placeholder {{{placeholder}}} in legal document {name!r}"
                )
    return doc
