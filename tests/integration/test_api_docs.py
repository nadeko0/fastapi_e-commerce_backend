"""Interactive docs and the OpenAPI schema per environment (F-18).

The FastAPI app is built once, at import of app.main, from the settings of
that moment, so production is looked at in a subprocess of its own.
"""

import os
import subprocess
import sys

from app.core.config import settings

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PROBE = """
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
for path in ("/api/v1/docs", "/api/v1/openapi.json", "/redoc", "/docs", "/openapi.json"):
    print(path, client.get(path).status_code)
"""

# Secrets that pass the production checks (app/core/config.py).
_PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    "JWT_SECRET_KEY": "k" * 64,
    "POSTGRES_PASSWORD": "p" * 24,
    "REDIS_PASSWORD": "r" * 24,
    "SMTP_PASSWORD": "an-smtp-app-password",
    "STRIPE_WEBHOOK_SECRET": "whsec_" + "w" * 32,
    "PAYMENT_PROVIDER": "stripe",
    "FRONTEND_URL": "https://shop.example",
    "STRIPE_SECRET_KEY": "stripe-secret-key-for-tests-not-a-real-key",
}


def _status_codes(env_overrides):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_ROOT,
        env={**os.environ, **env_overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split() for line in result.stdout.splitlines())


def test_production_serves_no_docs_and_no_schema():
    codes = _status_codes(_PRODUCTION_ENV)
    assert codes == {path: "404" for path in codes}, codes


def test_development_keeps_the_docs(client):
    assert settings.ENVIRONMENT == "development"
    assert client.get("/api/v1/docs").status_code == 200
    assert client.get("/api/v1/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 200
