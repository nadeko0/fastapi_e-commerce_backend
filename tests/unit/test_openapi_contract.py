"""What the OpenAPI document promises beyond its shape (tests/unit/test_openapi_snapshot.py
pins the shape): operationIds, the values of free-text response fields, response
fields that are always present, the 429 and the admin marker."""
import pytest

from app.main import app
from app.schemas.address import AddressCreate
from app.schemas.order import CheckoutSessionCreate, OrderCreate
from app.schemas.user import Token, UserCreate

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}


@pytest.fixture(scope="module")
def document():
    return app.openapi()


def _operations(document):
    return [
        (method.upper(), path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def _enum_of(schema: dict) -> list | None:
    """The enum of a string property, whether it sits in the property or in the
    non-null branch of its anyOf."""
    if "enum" in schema:
        return schema["enum"]
    for option in schema.get("anyOf", []):
        if "enum" in option:
            return option["enum"]
    return None


def test_operation_ids_are_unique_and_short():
    ids = [operation["operationId"] for _, _, operation in _operations(app.openapi())]
    assert len(ids) == len(set(ids))
    assert max(len(i) for i in ids) < 60
    # The handler's name, with the method where one handler serves a PUT and a PATCH.
    assert "get_product" in ids
    assert {"update_product_put", "update_product_patch"} <= set(ids)


@pytest.mark.parametrize(
    ("schema", "field", "values"),
    [
        ("OrderPayment", "status", {"processing", "requires_action", "unknown", "succeeded", "failed"}),
        ("AdminPaymentResponse", "status", {"processing", "requires_action", "unknown", "succeeded", "failed"}),
        ("AdminPaymentResponse", "provider", {"stripe"}),
        ("OrderPaymentRefund", "status", {"pending", "requires_action", "succeeded", "failed", "canceled"}),
        ("OrderPaymentRefund", "source", {"admin", "provider"}),
        ("OrderPaymentDispute", "status", {"won", "lost", "prevented", "needs_response", "under_review",
                                           "warning_needs_response", "warning_under_review", "warning_closed"}),
        ("RefundResponse", "status", {"pending", "succeeded", "failed"}),
        ("CheckoutSessionResponse", "status", {"pending", "requires_action", "paid", "failed"}),
        ("WebhookEventResponse", "status", {"received", "processing", "processed", "retrying", "failed"}),
        ("ConsentUpdateResponse", "status", {"success"}),
        ("DataRequestResponse", "status", {"processing"}),
        ("Problem", "type", None),
    ],
)
def test_free_text_response_fields_publish_their_values(document, schema, field, values):
    enum = _enum_of(document["components"]["schemas"][schema]["properties"][field])
    assert enum is not None
    if values is not None:
        assert set(enum) == values
    else:
        assert enum[0] == "about:blank" and len(enum) == 13


def test_dispute_reason_is_published_and_nullable(document):
    reason = document["components"]["schemas"]["OrderPaymentDispute"]["properties"]["reason"]
    assert "fraudulent" in _enum_of(reason)
    assert {"type": "null"} in reason["anyOf"]


def test_a_response_field_with_a_default_is_required(document):
    """A response always carries every field: none is optional in the document."""
    schemas = document["components"]["schemas"]
    for name in ("CartResponse", "CartResponseItem", "ProductResponse", "PaymentResponse",
                 "ProductVariantResponse", "AddressResponse", "UserResponse", "Token"):
        required = set(schemas[name].get("required", []))
        assert set(schemas[name]["properties"]) == required, name


def test_a_request_field_with_a_default_stays_optional(document):
    schemas = document["components"]["schemas"]
    assert "marketing_consent" not in schemas["UserCreate"]["required"]
    assert "is_default" not in schemas["AddressCreate"]["required"]
    assert "currency" not in schemas["PaymentCreate"]["required"]


def test_admin_payment_amounts_say_they_are_minor_units(document):
    props = document["components"]["schemas"]["AdminPaymentResponse"]["properties"]
    assert props["amount"]["type"] == "integer" and "minor units" in props["amount"]["description"]
    assert props["amount_refunded"]["type"] == "integer"


def test_every_throttled_operation_documents_429_and_the_webhook_does_not(document):
    for method, path, operation in _operations(document):
        responses = operation["responses"]
        if path in {"/health", "/livez", "/readyz"} or path.endswith("/orders/webhooks/stripe"):
            assert "429" not in responses, (method, path)
        else:
            assert "429" in responses, (method, path)
            assert "Retry-After" in responses["429"]["headers"], (method, path)


def test_admin_operations_are_marked(document):
    marked = {
        (method, path)
        for method, path, operation in _operations(document)
        if operation.get("x-admin-only") is True
    }
    admin_paths = {(m, p) for m, p, _ in _operations(document) if "/admin/" in p}
    assert admin_paths <= marked
    assert ("PUT", "/api/v1/orders/{order_id}/status") in marked
    assert ("POST", "/api/v1/orders/{order_id}/refund") in marked
    assert ("GET", "/api/v1/orders/{order_id}/payments") in marked
    assert ("GET", "/api/v1/products") not in marked


def test_headers_the_code_sets_are_documented(document):
    paths = document["paths"]
    assert "ETag" in paths["/api/v1/products/{product_id}"]["get"]["responses"]["200"]["headers"]
    assert "ETag" in paths["/api/v1/products/{product_id}"]["get"]["responses"]["304"]["headers"]
    assert "ETag" in paths["/api/v1/admin/products/{product_id}"]["put"]["responses"]["200"]["headers"]
    assert "ETag" in paths["/api/v1/admin/products/{product_id}"]["patch"]["responses"]["200"]["headers"]
    assert "Set-Cookie" in paths["/api/v1/cart/items"]["post"]["responses"]["200"]["headers"]
    assert "Set-Cookie" in paths["/api/v1/users/login"]["post"]["responses"]["200"]["headers"]
    for _, _, operation in _operations(document):
        assert all("X-Request-ID" in response["headers"] for response in operation["responses"].values())


def test_the_probes_have_a_response_schema_and_a_tag(document):
    for path in ("/health", "/livez", "/readyz"):
        operation = document["paths"][path]["get"]
        assert operation["tags"] == ["health"]
        assert "$ref" in operation["responses"]["200"]["content"]["application/json"]["schema"]


def test_tags_are_lower_case_and_described(document):
    declared = {tag["name"] for tag in document["tags"]}
    used = {tag for _, _, operation in _operations(document) for tag in operation["tags"]}
    assert used == declared
    assert all(name == name.lower() for name in declared)
    assert all(tag["description"] for tag in document["tags"])


@pytest.mark.parametrize("model", [UserCreate, AddressCreate, OrderCreate, CheckoutSessionCreate, Token])
def test_the_examples_are_valid_values(model):
    for example in model.model_json_schema()["examples"]:
        model.model_validate(example)
