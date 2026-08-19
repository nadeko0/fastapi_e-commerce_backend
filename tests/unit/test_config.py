import os
import subprocess
import sys

import pytest

# These run Settings() construction in a clean subprocess rather than
# importing app.core.config in-process: pydantic-settings reads
# BACKEND_CORS_ORIGINS at class-instantiation time (module import time for
# the app's own `settings = Settings()`), and the app is already imported
# with a fixed env by the time this test module loads - a subprocess is the
# only way to exercise different raw env-var values against Settings().
_BASE_ENV = {
    "JWT_SECRET_KEY": "x",
    "POSTGRES_SERVER": "x",
    "POSTGRES_USER": "x",
    "POSTGRES_PASSWORD": "x",
    "POSTGRES_DB": "x",
    "REDIS_HOST": "x",
    "DATA_ENCRYPTION_KEY": "x",
    "SMTP_HOST": "x",
    "SMTP_USER": "x@x.com",
    "SMTP_PASSWORD": "x",
    "EMAILS_FROM_EMAIL": "x@x.com",
    "EMAILS_FROM_NAME": "x",
}


def _settings_cors_origins(backend_cors_origins):
    env = {**os.environ, **_BASE_ENV}
    if backend_cors_origins is not None:
        env["BACKEND_CORS_ORIGINS"] = backend_cors_origins
    else:
        env.pop("BACKEND_CORS_ORIGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.BACKEND_CORS_ORIGINS)"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result


@pytest.mark.parametrize(
    "raw_value",
    [
        None,  # unset - the field's default
        "",  # empty string - a real deploy left this unset in .env
        "http://localhost:3000,http://localhost:8080",  # .env.example's own documented format
        '["http://localhost:3000"]',  # JSON list, also valid
    ],
)
def test_backend_cors_origins_does_not_crash_settings_construction(raw_value):
    # Regression test: pydantic-settings' default env-var handling for any
    # "complex" (list) field type runs the raw env string through
    # json.loads() before pydantic validation ever sees it, regardless of
    # whether the value looks like JSON. Without the NoDecode annotation on
    # this field (app/core/config.py), Settings() raised a raw
    # SettingsError/JSONDecodeError at process startup for every case above
    # except a real JSON array - including .env.example's own documented
    # comma-separated format. This crashed the app on every real deploy
    # (confirmed by actually building and running the Docker image) and was
    # invisible to the rest of the test suite, which never sets this env
    # var at all (conftest.py doesn't set it, so it silently used the
    # in-process default instead of going through env parsing).
    result = _settings_cors_origins(raw_value)
    assert result.returncode == 0, result.stderr
