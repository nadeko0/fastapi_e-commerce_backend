"""What the OpenAPI document says about errors, security and inputs (F-17, F-19).

The documented bodies are the ones the API sends; the runtime behaviour the
document describes is checked next to it where the document changed a
declaration (the webhook header, the admin filters, GET /products).
"""

import pytest
from payment_support import Shop, reset_provider

from app.api.v1.cart import guest_cart_cookie_name
from app.models.category import Category
from app.models.payment import WEBHOOK_EVENT_STATUSES
from app.models.product import Product
from app.schemas.order import OrderStatus, PaymentStatus

API = "/api/v1"


@pytest.fixture
def spec(client):
    return client.get(f"{API}/openapi.json").json()


def _op(spec, method, path):
    return spec["paths"][f"{API}{path}"][method]


def _ref(response):
    assert list(response["content"]) == ["application/problem+json"]
    return response["content"]["application/problem+json"]["schema"]["$ref"].rsplit("/", 1)[1]


def test_secured_operations_document_401_with_the_challenge(spec):
    for method, path in [("get", "/users/me"), ("get", "/admin/stats"), ("post", "/orders")]:
        response = _op(spec, method, path)["responses"]["401"]
        assert _ref(response) == "Problem"
        assert "WWW-Authenticate" in response["headers"]


def test_public_operations_do_not_document_a_token_401(spec):
    assert "401" not in _op(spec, "post", "/orders/webhooks/stripe")["responses"]
    assert "401" not in _op(spec, "get", "/products")["responses"]
    assert "401" not in _op(spec, "post", "/users/register")["responses"]


def test_admin_operations_document_403(spec):
    assert _ref(_op(spec, "get", "/admin/stats")["responses"]["403"]) == "Problem"


def test_error_bodies_are_problem_models(spec):
    schemas = spec["components"]["schemas"]
    assert {"type", "title", "status", "detail", "instance", "request_id", "retry_after"} == set(
        schemas["Problem"]["properties"]
    )
    # `type` defaults to about:blank (RFC 9457 section 3.1.1).
    assert set(schemas["Problem"]["required"]) == {"title", "status", "instance", "request_id"}
    assert "errors" in schemas["ValidationProblem"]["properties"]
    item = schemas["ValidationErrorItem"]["properties"]
    assert "input" not in item
    assert {"type", "loc", "msg"} <= set(item)
    for old in ("ErrorMessage", "RateLimitMessage", "RateLimitDetail", "HTTPValidationError", "ValidationError"):
        assert old not in schemas


def test_validation_error_is_the_sanitised_list_or_a_message(spec):
    schema = _op(spec, "post", "/users/register")["responses"]["422"]["content"]["application/problem+json"]["schema"]
    assert {ref["$ref"].rsplit("/", 1)[1] for ref in schema["anyOf"]} == {
        "ValidationProblem", "Problem"}


def test_429_and_503_document_retry_after(spec):
    for method, path, code in [("post", "/users/login", "429"), ("post", "/cart/items", "429"),
                               ("post", "/users/login", "503"), ("get", "/products", "503")]:
        response = _op(spec, method, path)["responses"][code]
        assert "Retry-After" in response["headers"], (path, code)
    assert _ref(_op(spec, "post", "/users/login")["responses"]["429"]) == "Problem"


def test_500_is_a_problem(spec):
    content = _op(spec, "get", "/products")["responses"]["500"]["content"]
    assert list(content) == ["application/problem+json"]
    assert content["application/problem+json"]["schema"]["$ref"].endswith("/Problem")


def test_json_body_operations_document_400_and_415_with_accept(spec):
    operation = _op(spec, "post", "/users/register")["responses"]
    assert _ref(operation["400"]) == "Problem"
    assert _ref(operation["415"]) == "Problem"
    assert "Accept" in operation["415"]["headers"]
    # A form body (the login) has no JSON to get wrong.
    assert "415" not in _op(spec, "post", "/users/login")["responses"]


def test_no_error_response_is_application_json(spec):
    for path in spec["paths"].values():
        for operation in path.values():
            for code, response in operation["responses"].items():
                if int(code) >= 400:
                    assert list(response["content"]) == ["application/problem+json"], code


def test_413_on_operations_that_take_a_body_only(spec):
    assert "413" in _op(spec, "post", "/users/register")["responses"]
    assert "413" in _op(spec, "post", "/orders/webhooks/stripe")["responses"]
    assert "413" not in _op(spec, "get", "/products")["responses"]


def test_a_routes_own_description_is_kept(spec):
    response = _op(spec, "post", "/cart/items")["responses"]["429"]
    assert "guest carts" in response["description"]
    assert _ref(response) == "Problem"


def test_cart_security_is_optional_and_the_guest_cookie_is_documented(spec):
    for method, path in [("get", "/cart"), ("post", "/cart/items"), ("delete", "/cart")]:
        operation = _op(spec, method, path)
        assert {} in operation["security"]
        assert {"OAuth2PasswordBearer": []} in operation["security"]
        cookies = [p for p in operation["parameters"] if p["in"] == "cookie"]
        assert [(p["name"], p["required"]) for p in cookies] == [(guest_cart_cookie_name(), False)]
    # A route that needs an account stays required.
    assert {} not in _op(spec, "get", "/users/me")["security"]


def test_admin_filters_list_the_values_the_routes_accept(spec):
    def enum_of(path, name):
        (parameter,) = [p for p in _op(spec, "get", path)["parameters"] if p["name"] == name]
        return next(s for s in parameter["schema"]["anyOf"] if "enum" in s)["enum"]

    assert enum_of("/admin/orders", "status") == [s.value for s in OrderStatus]
    assert enum_of("/admin/orders", "payment_status") == [s.value for s in PaymentStatus]
    assert enum_of("/admin/webhook-events", "status") == list(WEBHOOK_EVENT_STATUSES)


# The filters are plain str parameters with the enum in the schema only: the
# 422 stays the route's message, not FastAPI's list, and the match stays
# case-insensitive.
@pytest.fixture
def shop(client, db_session):
    reset_provider()
    yield Shop(client, db_session)
    reset_provider()


@pytest.mark.parametrize("path, params", [
    ("/admin/orders", {"status": "shipped"}),
    ("/admin/orders", {"payment_status": "free"}),
    ("/admin/webhook-events", {"status": "bogus"}),
])
def test_an_unknown_filter_value_is_still_a_422_message(shop, path, params):
    response = shop.client.get(f"{API}{path}", params=params, headers=shop.admin())

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert "Valid values are" in response.json()["detail"]


def test_filter_values_are_still_case_insensitive_for_orders(shop):
    response = shop.client.get(f"{API}/admin/orders", params={"status": "NEW"}, headers=shop.admin())

    assert response.status_code == 200


def test_stripe_signature_is_required_in_the_schema(spec):
    parameters = _op(spec, "post", "/orders/webhooks/stripe")["parameters"]
    (header,) = [p for p in parameters if p["name"].lower() == "stripe-signature"]
    assert header["in"] == "header"
    assert header["required"] is True


def test_a_missing_signature_is_still_a_400_not_a_422(client):
    response = client.post(f"{API}/orders/webhooks/stripe", content=b"{}")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid webhook signature"


def test_products_list_has_no_request_body(spec):
    operation = _op(spec, "get", "/products")
    assert "requestBody" not in operation
    (parameter,) = [p for p in operation["parameters"] if p["name"] == "characteristics"]
    assert parameter["in"] == "query"
    assert parameter["required"] is False


def _product(db_session, name, characteristics):
    category = db_session.query(Category).first()
    if category is None:
        category = Category(name="Things", parent_id=None, level=0, path=[])
        db_session.add(category)
        db_session.commit()
    db_session.add(Product(
        name=name, description="A fine thing for all your needs", price="9.99",
        stock_quantity=1, images=["https://example.com/p.png"], characteristics=characteristics,
        category_id=category.id,
    ))
    db_session.commit()


def test_products_filter_by_characteristics_query(client, db_session):
    _product(db_session, "Red", {"color": {"name": "Color", "value": "red"}})
    _product(db_session, "Blue", {"color": {"name": "Color", "value": "blue"}})

    everything = client.get(f"{API}/products").json()
    red = client.get(f"{API}/products", params={"characteristics": '{"color": "red"}'})

    assert everything["total"] == 2
    assert red.status_code == 200
    assert [item["name"] for item in red.json()["items"]] == ["Red"]


def test_a_body_on_get_products_is_not_a_filter(client, db_session):
    _product(db_session, "Red", {"color": {"name": "Color", "value": "red"}})
    _product(db_session, "Blue", {"color": {"name": "Color", "value": "blue"}})

    response = client.request("GET", f"{API}/products", json={"color": "red"})

    assert response.status_code == 200
    assert response.json()["total"] == 2


@pytest.mark.parametrize("value", ["not json", "[1, 2]", '"red"', "7", "x" * 1001])
def test_characteristics_must_be_a_json_object(client, value):
    response = client.get(f"{API}/products", params={"characteristics": value})

    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["loc"] == ["query", "characteristics"]
    assert "input" not in error


def test_a_document_rebuilt_after_the_routes_changed_still_says_problem_json(client):
    # FastAPI rebuilds the cached document when the router's routes change
    # (tests add and pop a probe route on the shared app); the rebuilt
    # document must be converted like the first one.
    from app.main import app

    app.openapi()

    @app.get(f"{API}/_openapi_probe")
    def probe():
        return {}

    try:
        rebuilt = app.openapi()
    finally:
        app.router.routes.pop()

    for path in rebuilt["paths"].values():
        for operation in path.values():
            for code, response in operation["responses"].items():
                if int(code) >= 400 and "application/json" in response.get("content", {}):
                    pytest.fail(f"{code} still documents application/json")
