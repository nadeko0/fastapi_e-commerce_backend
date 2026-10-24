"""The status code of each outcome is the one RFC 9110 defines for it.

One test per rule of 4.0.0's status-code change:

- a POST that creates a resource with a URL of its own answers 201 with a
  Location header naming it (RFC 9110 9.3.3, 15.3.2);
- registration, which accepts the request whatever the outcome, answers 202
  (15.3.3);
- DELETE answers 204 without a body (9.3.5, 15.3.5), and a second DELETE of
  what is gone is a 404;
- a request that conflicts with the current state of the resource is a 409
  (15.5.10), not a 400;
- a deactivated account with a valid token is a 403 `account-inactive`;
- a declined card is a 422 `payment-declined`, not 402 (15.5.3: reserved);
- a missing or malformed Idempotency-Key is a 400 `idempotency-key-missing`
  and a request of the same key still in flight a 409
  `idempotency-key-in-use` (draft-ietf-httpapi-idempotency-key-header,
  section 2.7).
"""
import pytest
from payment_support import (
    PAY_DECLINED,
    Outbox,
    Shop,
    new_key,
    order_state,
    reset_provider,
)

from app.core.config import settings
from app.core.errors import ProblemType
from app.models.address import Address
from app.models.payment import Payment
from app.models.user import User

API = settings.API_V1_STR
BASE = "https://github.com/nadeko0/fastapi_e-commerce_backend/problems/"


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    Outbox(monkeypatch)
    return Shop(client, db_session)


def assert_problem(response, status, problem_type=None):
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == (problem_type or "about:blank")
    return body


# -- 201 Created with Location -------------------------------------------------


def test_placing_an_order_is_201_with_the_location_of_the_order(shop):
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, shop.product()).status_code == 200

    response = shop.place_order(buyer)

    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert location == f"{API}/orders/{response.json()['id']}"
    assert shop.client.get(location, headers=buyer.headers).json() == response.json()


def test_replaying_an_order_request_repeats_the_201_and_the_location(shop):
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, shop.product()).status_code == 200
    key = new_key()

    first = shop.place_order(buyer, key=key)
    replay = shop.place_order(buyer, key=key)

    assert (first.status_code, replay.status_code) == (201, 201)
    assert replay.headers["Location"] == first.headers["Location"]
    assert replay.json() == first.json()


def test_creating_a_category_is_201_with_its_location(shop):
    admin = shop.admin()

    response = shop.client.post(f"{API}/admin/categories", json={"name": "Hats"}, headers=admin)

    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert location == f"{API}/categories/{response.json()['id']}"
    assert shop.client.get(location).status_code == 200


def test_creating_a_product_is_201_with_its_location(shop):
    admin = shop.admin()
    category = shop.client.post(
        f"{API}/admin/categories", json={"name": "Hats"}, headers=admin
    ).json()

    response = shop.client.post(
        f"{API}/admin/products",
        json={"name": "Cap", "description": "A baseball cap", "price": "9.99",
              "stock_quantity": 3, "category_id": category["id"],
              "images": ["https://img.test/cap.png"]},
        headers=admin,
    )

    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert location == f"{API}/products/{response.json()['id']}"
    assert shop.client.get(location).status_code == 200


def test_creating_a_variant_is_201_with_its_location(shop):
    admin = shop.admin()
    product = shop.product()

    response = shop.client.post(
        f"{API}/admin/products/{product.id}/variants",
        json={"sku": "CAP-RED-M", "attributes": {"size": "M"}, "stock_quantity": 2},
        headers=admin,
    )

    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert location == f"{API}/admin/products/{product.id}/variants/{response.json()['id']}"
    assert shop.client.get(location, headers=admin).status_code == 200


def test_creating_an_address_is_201_with_the_location_of_the_address(shop):
    buyer = shop.buyer()

    response = shop.client.post(
        f"{API}/users/addresses",
        json={"street": "9 Elm Street", "city": "Gotham", "state": "NY",
              "postal_code": "10002", "country": "US"},
        headers=buyer.headers,
    )

    assert response.status_code == 201, response.text
    location = response.headers["Location"]
    assert location == f"{API}/users/addresses/{response.json()['id']}"
    followed = shop.client.get(location, headers=buyer.headers)
    assert followed.status_code == 200
    assert followed.json() == response.json()


def test_posts_without_a_url_of_their_own_for_what_they_make_stay_200(shop):
    # No GET names a payment attempt, a checkout session or a cart line: a 201
    # without a Location would identify the target URI (RFC 9110 15.3.2),
    # which is the collection or the action, not the thing.
    buyer = shop.buyer()
    product = shop.product()
    added = shop.add_to_cart(buyer, product)
    assert added.status_code == 200 and "Location" not in added.headers
    order = shop.place_order(buyer).json()
    pay = shop.pay(buyer, order["id"])
    assert pay.status_code == 200 and "Location" not in pay.headers


# -- GET /users/addresses/{id} --------------------------------------------------


def test_getting_an_own_address_is_200_with_the_body_of_the_list_item(shop):
    buyer = shop.buyer()

    response = shop.client.get(f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers)

    assert response.status_code == 200, response.text
    items = shop.client.get(f"{API}/users/addresses", headers=buyer.headers).json()["items"]
    assert response.json() == items[0]
    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers


def test_getting_the_address_of_another_user_is_404(shop):
    owner = shop.buyer()
    other = shop.buyer()

    response = shop.client.get(f"{API}/users/addresses/{owner.address_id}", headers=other.headers)

    assert_problem(response, 404)


def test_getting_a_deleted_address_is_404(shop):
    buyer = shop.buyer()
    shop.client.delete(f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers)

    response = shop.client.get(f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers)

    assert_problem(response, 404)


def test_getting_a_missing_address_is_404(shop):
    buyer = shop.buyer()

    assert_problem(shop.client.get(f"{API}/users/addresses/2147483647", headers=buyer.headers), 404)


def test_head_of_an_own_address_is_200_without_a_body(shop):
    buyer = shop.buyer()
    url = f"{API}/users/addresses/{buyer.address_id}"

    got = shop.client.get(url, headers=buyer.headers)
    head = shop.client.head(url, headers=buyer.headers)

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == got.headers["content-length"]


# -- 202 Accepted ---------------------------------------------------------------


def test_registration_is_202_for_a_new_and_for_an_existing_email(client, valid_registration_payload):
    first = client.post(f"{API}/users/register", json=valid_registration_payload)
    again = client.post(f"{API}/users/register", json=valid_registration_payload)

    assert (first.status_code, again.status_code) == (202, 202)
    assert first.json() == again.json()


# -- 204 No Content -------------------------------------------------------------


def test_deleting_an_address_is_204_without_a_body_and_then_404(shop):
    buyer = shop.buyer()

    first = shop.client.delete(f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers)
    second = shop.client.delete(f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers)

    assert first.status_code == 204
    assert first.content == b""
    assert "content-type" not in first.headers
    assert_problem(second, 404)


def test_deleting_a_product_is_204_without_a_body(shop):
    admin = shop.admin()
    product = shop.product()

    response = shop.client.delete(f"{API}/admin/products/{product.id}", headers=admin)

    assert response.status_code == 204
    assert response.content == b""


def test_deleting_a_product_that_was_ordered_deactivates_it_with_204(shop):
    admin = shop.admin()
    product = shop.product()
    shop.order_of(shop.buyer(), product)

    response = shop.client.delete(f"{API}/admin/products/{product.id}", headers=admin)

    assert response.status_code == 204
    assert response.content == b""


# -- 409 Conflict ---------------------------------------------------------------


def test_an_invalid_order_status_transition_is_409(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    response = shop.set_status(shop.admin(), order["id"], "delivered")

    body = assert_problem(response, 409)
    assert body["detail"] == "Invalid status transition"


def test_cancelling_an_order_that_cannot_be_cancelled_is_409(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.cancel(buyer, order["id"]).status_code == 200

    assert_problem(shop.cancel(buyer, order["id"]), 409)


def test_paying_or_checking_out_an_order_that_is_paid_is_409(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.pay(buyer, order["id"]).status_code == 200

    assert_problem(shop.pay(buyer, order["id"]), 409)
    assert_problem(shop.checkout_session(buyer, order["id"]), 409)


def test_paying_an_order_that_is_cancelled_is_409(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.cancel(buyer, order["id"]).status_code == 200

    assert_problem(shop.pay(buyer, order["id"]), 409)


def test_refunding_an_order_that_is_not_paid_is_409(shop):
    order = shop.order_of(shop.buyer(), shop.product())

    assert_problem(shop.refund(shop.admin(), order["id"]), 409)


def test_adding_more_than_is_in_stock_is_409_insufficient_stock(shop):
    buyer = shop.buyer()
    product = shop.product(stock=2)

    response = shop.add_to_cart(buyer, product, quantity=3)

    assert_problem(response, 409, ProblemType.INSUFFICIENT_STOCK)
    assert response.json()["title"] == "Insufficient stock"


def test_raising_a_cart_line_above_the_stock_is_409_insufficient_stock(shop):
    buyer = shop.buyer()
    product = shop.product(stock=2)
    assert shop.add_to_cart(buyer, product, quantity=1).status_code == 200

    response = shop.client.put(
        f"{API}/cart/items/{product.id}", json={"quantity": 3}, headers=buyer.headers
    )

    assert_problem(response, 409, ProblemType.INSUFFICIENT_STOCK)


def test_placing_an_order_for_stock_that_is_gone_is_409_insufficient_stock(shop):
    buyer = shop.buyer()
    product = shop.product(stock=2)
    assert shop.add_to_cart(buyer, product, quantity=2).status_code == 200
    product.stock_quantity = 1
    shop.db.commit()

    response = shop.place_order(buyer)

    assert_problem(response, 409, ProblemType.INSUFFICIENT_STOCK)


def test_placing_an_order_for_a_product_withdrawn_from_sale_is_409(shop):
    buyer = shop.buyer()
    product = shop.product()
    assert shop.add_to_cart(buyer, product).status_code == 200
    product.is_active = False
    shop.db.commit()

    assert_problem(shop.place_order(buyer), 409)


def test_setting_an_inactive_address_as_default_is_409(shop):
    buyer = shop.buyer()
    inactive = Address(user_id=buyer.user_id, street="3 Old Street", city="Gotham",
                       state="NY", postal_code="10003", country="US", is_active=False)
    shop.db.add(inactive)
    shop.db.commit()

    response = shop.client.post(
        f"{API}/users/addresses/default", json={"address_id": inactive.id}, headers=buyer.headers
    )

    assert_problem(response, 409)


def test_updating_an_inactive_address_is_409(shop):
    buyer = shop.buyer()
    assert shop.client.delete(
        f"{API}/users/addresses/{buyer.address_id}", headers=buyer.headers
    ).status_code == 204

    response = shop.client.put(
        f"{API}/users/addresses/{buyer.address_id}", json={"city": "Gotham"}, headers=buyer.headers
    )

    assert_problem(response, 409)


def test_resending_the_verification_of_a_verified_email_is_409(shop):
    buyer = shop.buyer()

    response = shop.client.post(f"{API}/users/verify-email/resend", headers=buyer.headers)

    assert_problem(response, 409)


def test_an_input_error_stays_400(shop):
    # An empty cart and a currency that is not the order's are faults of the
    # request, not conflicts with a state.
    buyer = shop.buyer()
    assert_problem(shop.place_order(buyer), 400)
    order = shop.order_of(buyer, shop.product())
    assert_problem(shop.pay(buyer, order["id"], currency="EUR"), 400)


# -- 403 account-inactive -------------------------------------------------------


def test_an_inactive_account_with_a_valid_token_is_403_account_inactive(shop):
    buyer = shop.buyer()
    shop.db.get(User, buyer.user_id).is_active = False
    shop.db.commit()

    response = shop.client.get(f"{API}/users/me", headers=buyer.headers)

    body = assert_problem(response, 403, ProblemType.ACCOUNT_INACTIVE)
    assert body["title"] == "Account inactive"
    assert body["type"] == BASE + "account-inactive"
    assert "www-authenticate" not in response.headers


# -- 422 payment-declined -------------------------------------------------------


def test_a_declined_card_is_422_payment_declined(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    response = shop.pay(buyer, order["id"], token=PAY_DECLINED)

    body = assert_problem(response, 422, ProblemType.PAYMENT_DECLINED)
    assert body["title"] == "Payment declined"
    assert order_state(shop, order["id"]) == ("new", "failed")


# -- Idempotency-Key ------------------------------------------------------------


def _order_calls(shop, buyer, order_id):
    body = {"order_id": order_id, "payment_method": "stripe",
            "amount": str(shop.order(order_id).total_amount), "payment_method_token": "tok_visa"}
    return {
        "order": lambda headers: shop.client.post(
            f"{API}/orders", json={"shipping_address_id": buyer.address_id}, headers=headers),
        "pay": lambda headers: shop.client.post(
            f"{API}/orders/{order_id}/pay", json=body, headers=headers),
        "checkout": lambda headers: shop.client.post(
            f"{API}/orders/{order_id}/checkout-session", headers=headers),
        "refund": lambda headers: shop.client.post(
            f"{API}/orders/{order_id}/refund", json={}, headers=headers),
    }


@pytest.mark.parametrize("route", ["order", "pay", "checkout", "refund"])
def test_a_missing_idempotency_key_is_400_idempotency_key_missing(shop, route):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    headers = shop.admin() if route == "refund" else buyer.headers

    response = _order_calls(shop, buyer, order["id"])[route](headers)

    body = assert_problem(response, 400, ProblemType.IDEMPOTENCY_KEY_MISSING)
    assert body["title"] == "Idempotency-Key missing or invalid"
    assert "errors" not in body


@pytest.mark.parametrize("key", ["short", "x" * 256])
def test_a_malformed_idempotency_key_is_400_idempotency_key_missing(shop, key):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    response = _order_calls(shop, buyer, order["id"])["pay"]({**buyer.headers, "Idempotency-Key": key})

    assert_problem(response, 400, ProblemType.IDEMPOTENCY_KEY_MISSING)


def test_a_header_problem_wins_over_a_body_problem(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    response = shop.client.post(
        f"{API}/orders/{order['id']}/pay", json={"amount": "not money"}, headers=buyer.headers
    )

    assert_problem(response, 400, ProblemType.IDEMPOTENCY_KEY_MISSING)


def test_a_key_reused_with_another_payload_is_still_422(shop):
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, shop.product()).status_code == 200
    key = new_key()
    assert shop.place_order(buyer, key=key).status_code == 201
    other = Address(user_id=buyer.user_id, street="2 Side Street", city="Gotham",
                    state="NY", postal_code="10002", country="US")
    shop.db.add(other)
    shop.db.commit()

    response = shop.client.post(
        f"{API}/orders", json={"shipping_address_id": other.id},
        headers={**buyer.headers, "Idempotency-Key": key},
    )

    assert_problem(response, 422, ProblemType.IDEMPOTENCY_KEY_REUSED)


def _attempt_in_flight(shop, order_id, key, **columns):
    shop.db.add(Payment(
        order_id=order_id, provider="stripe", payment_intent_id=f"pending-{key}",
        idempotency_key=key, amount=1999, currency="USD", status="processing", **columns,
    ))
    shop.db.commit()


def test_pay_with_the_key_of_an_attempt_still_processing_is_409_in_use(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    key = new_key()
    _attempt_in_flight(shop, order["id"], key)

    response = shop.pay(buyer, order["id"], key=key)

    body = assert_problem(response, 409, ProblemType.IDEMPOTENCY_KEY_IN_USE)
    assert response.headers["Retry-After"] == "5"
    assert body["retry_after"] == 5


def test_checkout_session_with_the_key_of_an_attempt_still_processing_is_409_in_use(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    key = new_key()
    _attempt_in_flight(shop, order["id"], key)

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert_problem(response, 409, ProblemType.IDEMPOTENCY_KEY_IN_USE)
    assert response.headers["Retry-After"] == "5"


def test_a_replay_after_the_attempt_finished_returns_the_stored_result(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    key = new_key()
    first = shop.pay(buyer, order["id"], key=key)

    replay = shop.pay(buyer, order["id"], key=key)

    assert (first.status_code, replay.status_code) == (200, 200)
    # The token is not stored, so it is not echoed on the replay.
    assert {k: replay.json()[k] for k in ("id", "status", "transaction_id")} == {
        k: first.json()[k] for k in ("id", "status", "transaction_id")}
    assert "Retry-After" not in replay.headers


def test_checkout_session_with_the_key_of_a_failed_attempt_is_409_without_retry_after(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    key = new_key()
    _attempt_in_flight(shop, order["id"], key)
    shop.db.query(Payment).update({Payment.status: "failed"})
    shop.db.commit()

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert_problem(response, 409, ProblemType.IDEMPOTENCY_KEY_IN_USE)
    assert "Retry-After" not in response.headers


# -- OpenAPI --------------------------------------------------------------------


def _operation(client, method, path):
    return client.get(f"{API}/openapi.json").json()["paths"][f"{API}{path}"][method]


@pytest.mark.parametrize("method, path, documented, gone", [
    ("post", "/orders", {"201", "409"}, {"200"}),
    ("post", "/admin/categories", {"201"}, {"200"}),
    ("post", "/admin/products", {"201"}, {"200"}),
    ("post", "/admin/products/{product_id}/variants", {"201"}, {"200"}),
    ("post", "/users/register", {"202"}, {"200"}),
    ("delete", "/users/addresses/{address_id}", {"204", "404"}, {"200", "400"}),
    ("delete", "/admin/products/{product_id}", {"204"}, {"200"}),
    ("post", "/orders/{order_id}/cancel", {"409"}, {"400"}),
    ("put", "/orders/{order_id}/status", {"409"}, set()),
    ("post", "/orders/{order_id}/pay", {"409", "422"}, {"402"}),
    ("post", "/orders/{order_id}/checkout-session", {"409"}, set()),
    ("post", "/orders/{order_id}/refund", {"409"}, set()),
    ("post", "/cart/items", {"409"}, set()),
    ("put", "/cart/items/{product_id}", {"409"}, set()),
    ("post", "/users/addresses/default", {"409"}, set()),
    ("post", "/users/addresses", {"201"}, {"200"}),
    ("get", "/users/addresses/{address_id}", {"200", "404"}, set()),
])
def test_the_openapi_operation_lists_the_new_statuses(client, method, path, documented, gone):
    codes = set(_operation(client, method, path)["responses"])

    assert documented <= codes
    assert not gone & codes


@pytest.mark.parametrize("method, path", [
    ("get", "/users/me"),
    ("post", "/admin/products"),
    ("get", "/cart"),
    ("get", "/users/addresses/{address_id}"),
])
def test_operations_that_need_an_active_user_document_the_403_account_inactive(client, method, path):
    response = _operation(client, method, path)["responses"]["403"]

    assert "account-inactive" in response["description"]
    schema = response["content"]["application/problem+json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Problem"}


@pytest.mark.parametrize("method, path", [
    ("get", "/products"),
    ("post", "/users/register"),
    ("post", "/users/login"),
])
def test_public_operations_do_not_document_the_403_account_inactive(client, method, path):
    assert "403" not in _operation(client, method, path)["responses"]


def test_a_delete_204_response_has_no_content_in_the_document(client):
    for path in ("/users/addresses/{address_id}", "/admin/products/{product_id}"):
        assert "content" not in _operation(client, "delete", path)["responses"]["204"]


def test_the_idempotency_key_header_is_still_documented_as_required(client):
    for path in ("/orders", "/orders/{order_id}/pay"):
        parameters = _operation(client, "post", path)["parameters"]
        key = next(p for p in parameters if p["name"] == "Idempotency-Key")
        assert key["required"] is True


def test_reconciliation_run_documents_its_200_and_its_202_apart(client):
    responses = _operation(client, "post", "/admin/reconciliation/run")["responses"]

    ok = responses["200"]["content"]["application/json"]["schema"]
    accepted = responses["202"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ReconciliationRunResult"}
    assert accepted == {"$ref": "#/components/schemas/ReconciliationQueued"}


def test_the_queued_reconciliation_run_answers_202(shop, monkeypatch):
    from app.api.v1 import admin

    monkeypatch.setattr(admin.check_payment_reconciliation, "apply_async", lambda **kw: None)

    response = shop.client.post(f"{API}/admin/reconciliation/run", headers=shop.admin())

    assert response.status_code == 202
    assert response.json() == {"queued": True}
