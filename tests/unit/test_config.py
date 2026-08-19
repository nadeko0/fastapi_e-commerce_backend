import os
import subprocess
import sys

import pytest

# These run Settings() construction in a clean subprocess rather than
# importing app.core.config in-process: pydantic-settings reads these
# fields at class-instantiation time (module import time for the app's own
# `settings = Settings()`), and the app is already imported with a fixed
# env by the time this test module loads - a subprocess is the only way to
# exercise different raw env-var values against Settings().
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


def _construct_settings(env_var, raw_value):
    env = {**os.environ, **_BASE_ENV}
    if raw_value is not None:
        env[env_var] = raw_value
    else:
        env.pop(env_var, None)
    result = subprocess.run(
        [sys.executable, "-c", f"from app.core.config import settings; print(settings.{env_var})"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result


# Regression tests: pydantic-settings' default env-var handling for any
# "complex" (list) field type runs the raw env string through json.loads()
# before pydantic validation ever sees it, regardless of whether the value
# looks like JSON. Without a NoDecode annotation, every one of these three
# List-typed settings fields raised a raw SettingsError/JSONDecodeError at
# process startup - crashing the whole app - for an empty string and for
# a plain comma-separated value (the format .env.example itself documents
# for BACKEND_CORS_ORIGINS). Confirmed by actually building and running the
# Docker image against a real .env, not just running the local test suite,
# which never sets any of these three env vars and so never exercised the
# parsing path for any of them.
@pytest.mark.parametrize(
    "env_var,raw_value",
    [
        (env_var, raw_value)
        for env_var in ("BACKEND_CORS_ORIGINS", "TRUSTED_PROXIES", "ESSENTIAL_COOKIES")
        for raw_value in (
            None,  # unset - the field's default
            "",  # empty string - what an unfilled-in .env line becomes
            "10.0.0.1,10.0.0.2" if env_var != "BACKEND_CORS_ORIGINS" else
            "http://localhost:3000,http://localhost:8080",  # comma-separated
            '["a"]' if env_var != "BACKEND_CORS_ORIGINS" else
            '["http://localhost:3000"]',  # JSON list, also valid
        )
    ],
)
def test_list_setting_does_not_crash_settings_construction(env_var, raw_value):
    result = _construct_settings(env_var, raw_value)
    assert result.returncode == 0, result.stderr


def test_unknown_env_vars_from_shared_env_file_do_not_crash_settings():
    # Regression test: docker-compose.yml's api service loads the whole
    # .env file via env_file, which also contains docker-compose-only
    # variables (DOCKER_POSTGRES_PORT, DOCKER_REDIS_PORT,
    # DOCKER_NETWORK_NAME - used for host port mapping/network naming, not
    # read by the app itself). pydantic-settings' BaseSettings defaults to
    # forbidding unrecognized fields, so those leaked straight through as a
    # startup-crashing ValidationError - confirmed by actually deploying
    # the built Docker image to a real server, not just running the local
    # test suite (which sets only the exact env vars Settings expects and
    # so never exercised this path).
    env = {**os.environ, **_BASE_ENV}
    env["DOCKER_POSTGRES_PORT"] = "15432"
    env["DOCKER_REDIS_PORT"] = "16379"
    env["DOCKER_NETWORK_NAME"] = "some-network"
    result = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.PROJECT_NAME)"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
