"""Admin GET /api/v1/admin/products/{product_id} retrieves any product,
including deactivated ones, with the ETag needed for stock updates."""

import pytest
from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _order_line_for,
    _register_and_login,
    _valid_product_payload,
    _valid_registration_payload,
)

from app.models.product import Product


@pytest.fixture
def setup(client, db_session):
    admin = _make_admin(db_session)
    headers = _auth_headers(_login_as(client, admin.email, "AdminPass1"))
    category = _make_category(db_session)
    product = client.post(
        f"{ADMIN_PREFIX}/products",
        json={**_valid_product_payload(category.id), "price": "19.99", "stock_quantity": 25},
        headers=headers,
    ).json()
    return headers, product


def test_admin_get_active_product_without_variants(client, setup):
    headers, product = setup
    product_id = product["id"]

    public_resp = client.get(f"{CATALOG_PREFIX}/products/{product_id}")
    assert public_resp.status_code == 200

    admin_resp = client.get(f"{ADMIN_PREFIX}/products/{product_id}", headers=headers)
    assert admin_resp.status_code == 200
    assert admin_resp.json() == public_resp.json()
    assert admin_resp.headers["etag"] == public_resp.headers["etag"]


def test_admin_get_active_product_with_variants(client, setup):
    headers, product = setup
    product_id = product["id"]

    var_resp = client.post(
        f"{ADMIN_PREFIX}/products/{product_id}/variants",
        json={"sku": "VAR-ADMIN-GET-1", "stock_quantity": 10},
        headers=headers,
    )
    assert var_resp.status_code == 201

    public_resp = client.get(f"{CATALOG_PREFIX}/products/{product_id}")
    assert public_resp.status_code == 200

    admin_resp = client.get(f"{ADMIN_PREFIX}/products/{product_id}", headers=headers)
    assert admin_resp.status_code == 200
    assert admin_resp.json() == public_resp.json()
    assert admin_resp.headers["etag"] == public_resp.headers["etag"]


def test_admin_get_deactivated_product_and_update_stock(client, setup, db_session):
    headers, product = setup
    product_id = product["id"]

    # Deactivate the product via delete with an existing order line
    _order_line_for(db_session, product_id)
    del_resp = client.delete(f"{ADMIN_PREFIX}/products/{product_id}", headers=headers)
    assert del_resp.status_code == 204

    # Confirm it is deactivated in the DB and public GET is 404
    db_session.expire_all()
    assert db_session.get(Product, product_id).is_active is False
    public_resp = client.get(f"{CATALOG_PREFIX}/products/{product_id}")
    assert public_resp.status_code == 404

    # Admin GET answers 200 with an ETag
    admin_resp = client.get(f"{ADMIN_PREFIX}/products/{product_id}", headers=headers)
    assert admin_resp.status_code == 200
    etag = admin_resp.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")

    # PUT with stock_quantity and that ETag succeeds
    put_resp = client.put(
        f"{ADMIN_PREFIX}/products/{product_id}",
        json={"stock_quantity": 42},
        headers={**headers, "If-Match": etag},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["stock_quantity"] == 42

    # Stock is 42 afterwards when read through the admin GET
    admin_resp_after = client.get(f"{ADMIN_PREFIX}/products/{product_id}", headers=headers)
    assert admin_resp_after.status_code == 200
    assert admin_resp_after.json()["stock_quantity"] == 42


def test_admin_get_unknown_product_returns_404(client, setup):
    headers, _ = setup
    resp = client.get(f"{ADMIN_PREFIX}/products/999999", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Product not found"


def test_admin_get_product_requires_admin_privileges(client, setup):
    _, product = setup
    buyer_token = _register_and_login(client, _valid_registration_payload())
    resp = client.get(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        headers=_auth_headers(buyer_token),
    )
    assert resp.status_code == 403


def test_admin_get_product_requires_authentication(client, setup):
    _, product = setup
    resp = client.get(f"{ADMIN_PREFIX}/products/{product['id']}")
    assert resp.status_code == 401
