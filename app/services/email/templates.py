from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "email"

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template: str, **context: Any) -> str:
    """Render an email HTML template with autoescaping and strict undefined checks."""
    return _env.get_template(template).render(**context)
