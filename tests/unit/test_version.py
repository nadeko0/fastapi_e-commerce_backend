import tomllib
from pathlib import Path

import app
from app.core.config import Settings, settings

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_app_version_is_the_pyproject_version():
    expected = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert app.__version__ == expected
    assert settings.VERSION == expected


def test_version_cannot_be_overridden_by_the_environment(monkeypatch):
    monkeypatch.setenv("VERSION", "9.9.9")

    assert Settings().VERSION == app.__version__
