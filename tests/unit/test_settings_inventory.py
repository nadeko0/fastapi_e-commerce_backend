"""Every setting is read by the code, and .env.example names only what
something reads. A setting nothing reads is a knob that does nothing: an
operator changes it and nothing happens (RELOAD was one). A name in
.env.example that is neither a setting nor a docker-compose.yml variable
is a typo the application cannot catch: environment variables are read by
field name only, so a misspelled one is never seen."""

import re
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]

# Read only inside config.py: the parts DATABASE_URI is built from, and the
# old login setting that exists so that setting it fails startup.
_READ_IN_CONFIG = {
    "POSTGRES_SERVER", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "RATE_LIMIT_LOGIN_FAILURES",
}


def _code_outside_config() -> str:
    files = [p for p in (ROOT / "app").rglob("*.py") if p.name != "config.py"]
    return "\n".join(p.read_text(encoding="utf-8") for p in [*files, ROOT / "main.py"])


def test_every_setting_is_read_outside_config():
    code = _code_outside_config()
    unread = sorted(
        name for name in Settings.model_fields
        if name not in _READ_IN_CONFIG and not re.search(rf"\b{name}\b", code)
    )
    assert unread == []


def test_the_config_only_settings_are_still_settings():
    assert _READ_IN_CONFIG <= set(Settings.model_fields)


def test_env_example_names_only_settings_and_compose_variables():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_variables = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", example, re.MULTILINE))

    unknown = sorted(names - set(Settings.model_fields) - compose_variables)

    assert names, "no NAME=value line found in .env.example"
    assert unknown == []
