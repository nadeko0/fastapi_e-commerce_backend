"""F-22: no request body larger than MAX_REQUEST_BODY_BYTES is read, on any
route, including the unauthenticated ones that used to parse 30 MB."""

from app.core.config import settings

API = settings.API_V1_STR
OVER = settings.MAX_REQUEST_BODY_BYTES + 1
TOO_LARGE = "Request body is too large"


def _big_json(size):
    # A valid JSON document of exactly `size` bytes.
    return b'{"pad":"' + b"x" * (size - 10) + b'"}'


def test_default_limit_is_one_mebibyte():
    assert settings.MAX_REQUEST_BODY_BYTES == 1_048_576


def test_register_with_an_oversized_body_is_413_with_security_headers(client):
    response = client.post(f"{API}/users/register", content=_big_json(OVER),
                           headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json()["detail"] == TOO_LARGE
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_webhook_with_an_oversized_body_is_413_before_the_signature_check(client):
    # No Stripe-Signature at all: a body that was read would be a 400.
    response = client.post(f"{API}/orders/webhooks/stripe", content=_big_json(OVER))

    assert response.status_code == 413
    assert response.json()["detail"] == TOO_LARGE


def test_authenticated_endpoint_with_an_oversized_body_is_413(client):
    response = client.post(f"{API}/cart/items", content=_big_json(OVER),
                           headers={"Authorization": "Bearer not-checked-first",
                                    "Content-Type": "application/json"})

    assert response.status_code == 413


def test_chunked_oversized_body_is_413(client):
    def chunks():
        for _ in range(OVER // 65536 + 1):
            yield b"x" * 65536

    response = client.post(f"{API}/users/register", content=chunks(),
                           headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert response.json()["detail"] == TOO_LARGE


def test_body_at_the_limit_and_under_it_reach_the_app(client):
    # The app then rejects the content (422), which is "normal handling".
    for size in (settings.MAX_REQUEST_BODY_BYTES, settings.MAX_REQUEST_BODY_BYTES - 1):
        response = client.post(f"{API}/users/register", content=_big_json(size),
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 422


def test_get_without_a_body_is_unaffected(client):
    assert client.get(f"{API}/categories").status_code == 200
