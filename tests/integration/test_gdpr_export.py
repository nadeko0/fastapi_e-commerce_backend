import uuid

API_PREFIX = "/api/v1/users"


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def test_data_export_returns_personal_data_and_consents(client, valid_registration_payload):
    # Regression test: GET /users/data/export used to construct the
    # GDPRExport schema (export *metadata*: request_id/request_date/
    # expires_at) with personal_data/consents/addresses/orders kwargs that
    # don't exist on it, raising a pydantic ValidationError on every call.
    # The actual data container is GDPRExportData.
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.get(
        f"{API_PREFIX}/data/export",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["personal_data"]["email"] == valid_registration_payload["email"]
    assert isinstance(data["consents"], list)
    assert isinstance(data["addresses"], list)
    assert isinstance(data["orders"], list)
    assert data["export_metadata"]["request_id"]
    assert data["export_metadata"]["status"] == "completed"


def test_update_consent_grants_marketing_consent(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/consent",
        params={"consent_type": "marketing", "granted": True},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["email"] == valid_registration_payload["email"]


def test_update_consent_rejects_gdpr_revocation(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/consent",
        params={"consent_type": "gdpr", "granted": False},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 400


def test_data_delete_rejects_wrong_password(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/data/delete",
        json={"confirmation": True, "password": "wrong-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401


def test_data_delete_deactivates_account(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/data/delete",
        json={
            "confirmation": True,
            "password": valid_registration_payload["password"],
            "reason": "no longer needed",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert "deletion_date" in response.json()["data"]

    # Deactivated account can no longer authenticate: the token it held is
    # refused, and logging in again is refused like a wrong password.
    me_response = client.get(
        f"{API_PREFIX}/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_response.status_code == 400
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    assert login_response.status_code == 401
    assert login_response.json()["detail"] == "Incorrect email or password"



def _account_with_orders(client, db_session, payload, orders):
    """An account with one address and `orders` orders of one item each."""
    from decimal import Decimal

    from app.models.address import Address
    from app.models.category import Category
    from app.models.order import Order
    from app.models.order_items import OrderItem
    from app.models.product import Product
    from app.models.user import User

    access_token = _register_and_login(client, payload)
    user = db_session.query(User).filter(User.email == payload["email"]).one()
    category = Category(name=f"Cat {uuid.uuid4().hex[:6]}", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    address = Address(
        user_id=user.id, street="123 Main Street", city="Springfield", state="Illinois",
        postal_code="62701", country="US", is_default=True,
    )
    db_session.add(address)
    db_session.commit()
    product = Product(
        name=f"Widget {uuid.uuid4().hex[:6]}", description="A fine widget, long enough",
        price=Decimal("9.99"), stock_quantity=10, images=[], characteristics={},
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    for _ in range(orders):
        order = Order(
            user_id=user.id, status="new", payment_status="pending",
            shipping_address_id=address.id, total_amount=Decimal("9.99"),
        )
        db_session.add(order)
        db_session.commit()
        db_session.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=1, price_at_time=product.price,
        ))
        db_session.commit()
    return {"Authorization": f"Bearer {access_token}"}


def test_data_export_over_the_size_cap_is_refused_with_a_clear_answer(
    client, db_session, monkeypatch, valid_registration_payload
):
    # The export was loaded whole, however large: every address, order and
    # item into the worker's memory and into one response. Now it has a
    # ceiling, and past it the account holder is told how to get the data
    # instead (an Art. 15 request still has to be answered).
    from app.core.config import settings

    headers = _account_with_orders(client, db_session, valid_registration_payload, orders=2)
    # 1 address + 2 orders + 2 items = 5 records.
    monkeypatch.setattr(settings, "GDPR_EXPORT_MAX_RECORDS", 4, raising=False)

    response = client.get(f"{API_PREFIX}/data/export", headers=headers)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["records"] == 5
    assert detail["limit"] == 4
    assert settings.DPO_EMAIL in detail["error"]


def test_data_export_at_the_size_cap_is_served(
    client, db_session, monkeypatch, valid_registration_payload
):
    from app.core.config import settings

    headers = _account_with_orders(client, db_session, valid_registration_payload, orders=2)
    monkeypatch.setattr(settings, "GDPR_EXPORT_MAX_RECORDS", 5, raising=False)

    response = client.get(f"{API_PREFIX}/data/export", headers=headers)

    assert response.status_code == 200
    assert len(response.json()["data"]["orders"]) == 2


def test_data_export_query_count_does_not_grow_with_the_number_of_orders(
    client, db_session, valid_registration_payload
):
    """Regression test: the export read current_user.orders lazily and let
    OrderResponse walk items, products, variants and addresses one query at
    a time - 165 queries for one export, measured. Loaded eagerly it is a
    fixed number of queries however many orders the account has."""
    from decimal import Decimal

    from conftest import engine
    from sqlalchemy import event

    from app.models.address import Address
    from app.models.category import Category
    from app.models.order import Order
    from app.models.order_items import OrderItem
    from app.models.product import Product
    from app.models.user import User

    access_token = _register_and_login(client, valid_registration_payload)
    user = db_session.query(User).filter(User.email == valid_registration_payload["email"]).one()

    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    address = Address(
        user_id=user.id, street="123 Main Street", city="Springfield", state="Illinois",
        postal_code="62701", country="US", is_default=True,
    )
    db_session.add(address)
    db_session.commit()

    def add_orders(count):
        for _n in range(count):
            product = Product(
                name=f"Widget {uuid.uuid4().hex[:6]}", description="A fine widget, long enough",
                price=Decimal("9.99"), stock_quantity=10, images=[], characteristics={},
                category_id=category.id,
            )
            db_session.add(product)
            db_session.commit()
            order = Order(
                user_id=user.id, status="new", payment_status="pending",
                shipping_address_id=address.id, total_amount=Decimal("19.98"),
            )
            db_session.add(order)
            db_session.commit()
            db_session.add(OrderItem(
                order_id=order.id, product_id=product.id, quantity=2, price_at_time=product.price,
            ))
            db_session.commit()

    def export_query_count():
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get(
                f"{API_PREFIX}/data/export", headers={"Authorization": f"Bearer {access_token}"}
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert response.status_code == 200, response.text
        return len(statements), response.json()["data"]

    add_orders(2)
    with_two, data = export_query_count()
    assert len(data["orders"]) == 2

    add_orders(6)
    with_eight, data = export_query_count()
    assert len(data["orders"]) == 8
    assert [len(order["items"]) for order in data["orders"]] == [1] * 8

    assert with_eight == with_two, (
        f"{with_two} queries for 2 orders but {with_eight} for 8: something is loaded per order"
    )
