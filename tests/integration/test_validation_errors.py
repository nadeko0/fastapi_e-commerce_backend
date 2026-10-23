"""A 422 from request validation does not echo what the client sent.

FastAPI's default handler puts every error's `input` in the body: the
password of a refused registration, the new password of a refused reset,
a whole oversized body. Anything that records response bodies (a proxy, an
APM) would then store them. The app's handler (app/core/errors.py) removes
`input` and any `ctx` value that is not a constraint; the rest of each
error item is what FastAPI's default handler sends.
"""

import json

from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from app.main import app

USERS = "/api/v1/users"
SECRET = "Sup3rSecretValue"


def _default_422(client, method, url, **kwargs):
    """The body FastAPI's own handler produces for the same request."""
    ours = app.exception_handlers[RequestValidationError]
    app.exception_handlers[RequestValidationError] = request_validation_exception_handler
    app.middleware_stack = None  # rebuilt with the handler swapped in
    try:
        return client.request(method, url, **kwargs)
    finally:
        app.exception_handlers[RequestValidationError] = ours
        app.middleware_stack = None


def _register_payload(**changes):
    payload = {
        "email": "jane.doe@example.com",
        "password": "Str0ngPass",
        "full_name": "Jane Doe",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
    }
    payload.update(changes)
    return payload


def _has_key(value, key):
    if isinstance(value, dict):
        return key in value or any(_has_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_has_key(v, key) for v in value)
    return False


def _assert_same_but_input(client, method, url, secret=None, **kwargs):
    ours = client.request(method, url, **kwargs)
    theirs = _default_422(client, method, url, **kwargs)
    assert ours.status_code == theirs.status_code == 422
    assert not _has_key(ours.json(), "input")
    if secret:
        assert secret not in ours.text
        assert secret in theirs.text  # the default really does echo it
    mine, default = ours.json()["detail"], theirs.json()["detail"]
    assert len(mine) == len(default) > 0
    for got, want in zip(mine, default, strict=True):
        for key in ("type", "loc", "msg"):
            assert got[key] == want[key]
        assert set(got) <= {"type", "loc", "msg", "ctx", "url"}
    return ours


def test_weak_password_422_does_not_echo_it(client):
    response = _assert_same_but_input(
        client, "POST", f"{USERS}/register",
        secret="qwertyuiop", json=_register_payload(password="qwertyuiop"),
    )
    assert "too common" in response.text


def test_short_password_422_keeps_constraint_context(client):
    # PasswordUpdate.new_password has min_length=8; ctx carries the 8, not the input.
    response = _assert_same_but_input(
        client, "POST", f"{USERS}/password/reset/sometoken",
        secret="sh0rt", json={"current_password": "x", "new_password": "sh0rt"},
    )
    ctxs = [e["ctx"] for e in response.json()["detail"] if "ctx" in e]
    assert {"min_length": 8} in ctxs


def test_missing_field_422_does_not_echo_the_body(client):
    payload = _register_payload(password=SECRET)
    del payload["privacy_policy_accepted"]
    response = _assert_same_but_input(
        client, "POST", f"{USERS}/register", secret=SECRET, json=payload,
    )
    assert response.json()["detail"][0]["type"] == "missing"


def test_password_reset_422_does_not_echo_new_password(client):
    # current_password missing, new_password too weak: both are refused.
    _assert_same_but_input(
        client, "POST", f"{USERS}/password/reset/sometoken",
        secret="NoDigitsHere", json={"new_password": "NoDigitsHere"},
    )


def test_big_invalid_body_422_is_not_echoed_back(client):
    big = "A" * 1_000_000
    response = _assert_same_but_input(
        client, "POST", f"{USERS}/register",
        secret="A" * 1000, json=_register_payload(password=big),
    )
    assert len(response.content) < 5_000


def test_malformed_json_422_does_not_echo_it(client):
    body = '{"password": "' + SECRET + '", '
    response = client.post(
        f"{USERS}/register", content=body, headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert SECRET not in response.text
    assert not _has_key(response.json(), "input")
    assert response.json()["detail"][0]["type"] == "json_invalid"


def test_ctx_never_carries_a_submitted_value(client):
    # a value_error's ctx holds the exception object; its text is in `msg`
    response = client.post(f"{USERS}/register", json=_register_payload(password="qwertyuiop"))
    for error in response.json()["detail"]:
        assert "qwertyuiop" not in json.dumps(error)
        assert "error" not in error.get("ctx", {})
