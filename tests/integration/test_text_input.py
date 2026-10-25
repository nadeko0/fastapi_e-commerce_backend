"""Text a database or a log cannot hold is refused at the door (4.0.1).

A NUL character (U+0000) in a string input reached PostgreSQL, whose text
type cannot store it: psycopg raised DataError and the route answered 500
(SQLite stores it, so only a PostgreSQL run shows the failure). A lone
surrogate (U+D800..U+DFFF, what the JSON escape "\\ud800" decodes to) is not
a Unicode scalar value: it cannot be encoded as UTF-8, and PyJWT's
`token.encode("utf-8")` raised UnicodeEncodeError on one in
POST /users/refresh (500, unauthenticated).

Both are a 422 of type validation-error with a loc and a fixed msg, for every
kind of input: JSON bodies (nested objects, lists and member names included),
query parameters, path parameters and form fields. The value is never echoed.
A token with such a character is an invalid token (401), wherever it arrives.
"""
import json
from dataclasses import dataclass

import pytest
from payment_support import Shop
from route_support import probe_routes

from app.core.errors import PROBLEM_MEDIA_TYPE, ProblemType

API = "/api/v1"
NUL = "\x00"
SURROGATE = "\ud800"
NUL_MSG = "String must not contain NUL characters"
SURROGATE_MSG = "String must be valid Unicode"
MARK = "zzmarkzz"
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}


@dataclass
class World:
    shop: Shop
    admin: dict
    buyer: object
    product: object

    @property
    def client(self):
        return self.shop.client


@pytest.fixture
def world(client, db_session):
    shop = Shop(client, db_session)
    return World(shop, shop.admin(), shop.buyer(), shop.product())


def _raw(client, method, path, body, headers=None):
    """A JSON body with every non-ASCII character escaped, so a lone surrogate
    reaches the server as the JSON escape (httpx cannot encode it as UTF-8)."""
    return client.request(
        method, f"{API}{path}", content=json.dumps(body, ensure_ascii=True),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _address(**extra):
    return {"street": "123 Main Street", "city": "Springfield", "state": "Illinois",
            "postal_code": "62701", "country": "US", **extra}


def _register(**extra):
    return {"email": "text-input@example.com", "password": "Str0ngPass99",
            "full_name": "Text Input", "gdpr_consent": True,
            "privacy_policy_accepted": True, **extra}


def _product(w, **extra):
    return {"name": "Text Widget", "description": "A widget for the text tests",
            "price": "9.99", "stock_quantity": 1, "category_id": w.product.category_id,
            "images": ["https://example.com/w.png"], "characteristics": {}, **extra}


# (id, loc, request(world, bad) -> response). `bad` is the offending string,
# MARK-flanked so the test can search the response for it.
def _cases():
    def body(method, path, make, headers=None, admin=False, buyer=False):
        def send(w, bad):
            h = dict(headers or {})
            if admin:
                h.update(w.admin)
            if buyer:
                h.update(w.buyer.headers)
            return _raw(w.client, method, path(w) if callable(path) else path, make(w, bad), h)
        return send

    cases = [
        ("search-query", ("query", "query"),
         lambda w, bad: w.client.get(f"{API}/products/search", params={"query": bad})),
        ("login-username", ("body", "username"),
         lambda w, bad: w.client.post(f"{API}/users/login", data={"username": bad, "password": "x"})),
        ("register-full_name", ("body", "full_name"),
         body("POST", "/users/register", lambda w, bad: _register(full_name=bad))),
        ("refresh-token", ("body", "refresh_token"),
         body("POST", "/users/refresh", lambda w, bad: {"refresh_token": bad})),
        ("me-put-full_name", ("body", "full_name"),
         body("PUT", "/users/me", lambda w, bad: {"full_name": bad}, buyer=True)),
        ("me-patch-full_name", ("body", "full_name"),
         body("PATCH", "/users/me", lambda w, bad: {"full_name": bad}, MERGE_PATCH, buyer=True)),
        ("category-name", ("body", "name"),
         body("POST", "/admin/categories", lambda w, bad: {"name": bad}, admin=True)),
        ("product-name", ("body", "name"),
         body("POST", "/admin/products", lambda w, bad: _product(w, name=bad), admin=True)),
        ("variant-sku", ("body", "sku"),
         body("POST", lambda w: f"/admin/products/{w.product.id}/variants",
              lambda w, bad: {"sku": bad}, admin=True)),
        # Nested: a value inside an object inside an object, a member name,
        # an element of a list inside a free-form object.
        ("characteristic-value", ("body", "characteristics", "Color", "value"),
         body("POST", "/admin/products",
              lambda w, bad: _product(w, characteristics={"Color": {"name": "Color", "value": bad}}),
              admin=True)),
        ("characteristic-key", ("body", "characteristics"),
         body("POST", "/admin/products",
              lambda w, bad: _product(w, characteristics={bad: {"name": "Color", "value": "red"}}),
              admin=True)),
        ("variant-attributes-list", ("body", "attributes", "sizes", 1),
         body("POST", lambda w: f"/admin/products/{w.product.id}/variants",
              lambda w, bad: {"sku": "SKU-TEXT-1", "attributes": {"sizes": ["S", bad]}}, admin=True)),
        ("product-images-list", ("body", "images", 0),
         body("POST", "/admin/products", lambda w, bad: _product(w, images=[bad]), admin=True)),
    ]
    for field in ("street", "city", "state", "postal_code", "country", "delivery_instructions"):
        cases.append((
            f"address-{field}", ("body", field),
            body("POST", "/users/addresses", lambda w, bad, f=field: _address(**{f: bad}), buyer=True),
        ))
    return cases


CASES = _cases()


def _assert_refused(response, loc, message, bad):
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    problem = response.json()
    assert problem["type"] == ProblemType.VALIDATION_ERROR
    assert problem["status"] == 422
    errors = problem["errors"]
    assert [(tuple(e["loc"]), e["msg"]) for e in errors] == [(loc, message)]
    assert set(errors[0]) <= {"type", "loc", "msg", "ctx", "url"}
    # Never echoed: neither the character, its escape, nor the text around it.
    assert MARK not in response.text
    assert "\\u0000" not in response.text and "\\ud800" not in response.text


@pytest.mark.parametrize("name,loc,send", CASES, ids=[c[0] for c in CASES])
def test_nul_character_is_a_422(world, name, loc, send):
    response = send(world, f"{MARK}{NUL}{MARK}")
    _assert_refused(response, loc, NUL_MSG, NUL)


# Query and form values cannot carry a lone surrogate: they are percent-decoded
# as UTF-8 with replacement, so the character never arrives. It arrives only
# through a JSON escape.
JSON_CASES = [c for c in CASES if c[1][0] == "body" and c[0] != "login-username"]


@pytest.mark.parametrize("name,loc,send", JSON_CASES, ids=[c[0] for c in JSON_CASES])
def test_lone_surrogate_is_a_422(world, name, loc, send):
    response = send(world, f"{MARK}{SURROGATE}{MARK}")
    _assert_refused(response, loc, SURROGATE_MSG, SURROGATE)


def test_json_query_parameter_with_a_nul_escape_is_a_422(world):
    # The characteristics filter is JSON in a query parameter: the NUL is the
    # six characters \\u0000, decoded only when the filter is parsed.
    for raw, loc in (
        ('{"color": "a\\u0000b"}', ("query", "characteristics", "color")),
        ('{"a\\u0000b": "red"}', ("query", "characteristics")),
    ):
        response = world.client.get(f"{API}/products", params={"characteristics": raw})
        _assert_refused(response, loc, NUL_MSG, NUL)


def test_repeated_query_parameter_names_the_bad_value(world):
    response = world.client.get(f"{API}/categories", params={"tag": ["fine", NUL]})
    _assert_refused(response, ("query", "tag", 1), NUL_MSG, NUL)


def test_string_path_parameter_with_nul_is_a_422(client):
    # No route has a string path parameter today; one added later is covered.
    with probe_routes() as app:
        @app.get("/api/v1/text-probe/{name}")
        async def probe(name: str):
            return {"name": name}

        refused = client.get(f"{API}/text-probe/a%00b")
        assert client.get(f"{API}/text-probe/ab").status_code == 200
    _assert_refused(refused, ("path", "name"), NUL_MSG, NUL)


def test_valid_unicode_is_untouched(world):
    # Astral characters (a surrogate PAIR in JSON) and the replacement
    # character are valid text.
    response = _raw(world.client, "POST", "/admin/categories", {"name": "Tea \U0001F375 �"},
                    world.admin)
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Tea \U0001F375 �"
    pair = world.client.post(
        f"{API}/admin/categories", headers={**world.admin, "Content-Type": "application/json"},
        content=rb'{"name": "Pair \ud83c\udf75"}',
    )
    assert pair.status_code == 201, pair.text


def test_refresh_with_lone_surrogate_is_not_a_500(world):
    response = _raw(world.client, "POST", "/users/refresh", {"refresh_token": "a\ud800b"})
    assert response.status_code == 422
