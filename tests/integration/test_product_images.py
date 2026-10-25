"""A product without images.

The request schema demands at least one image. Pydantic does not run
validators on a default, so a create that left `images` out used to slip past
that rule: the row was stored with no images, building the response then
failed (500), and the public catalog answered 500 for every read of the row.
"""
import pytest
from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)

from app.models.product import Product


def _admin_headers(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def test_a_create_without_images_is_refused_like_an_empty_list(client, db_session):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    omitted = _valid_product_payload(category.id)
    del omitted["images"]
    empty = {**omitted, "images": []}

    answer_omitted = client.post(f"{ADMIN_PREFIX}/products", json=omitted, headers=headers)
    answer_empty = client.post(f"{ADMIN_PREFIX}/products", json=empty, headers=headers)

    assert answer_empty.status_code == 422
    assert answer_omitted.status_code == 422
    assert [e["loc"] for e in answer_omitted.json()["errors"]] == [["body", "images"]]
    assert db_session.query(Product).count() == 0


def _product_without_images(db_session):
    category = _make_category(db_session)
    product = Product(
        name="Old Widget", description="A widget stored before images were required",
        price="9.99", stock_quantity=3, category_id=category.id, images=[], characteristics={},
    )
    db_session.add(product)
    db_session.commit()
    return category, product


def test_a_row_without_images_does_not_break_the_public_catalog(client, db_session):
    category, product = _product_without_images(db_session)

    listing = client.get(f"{CATALOG_PREFIX}/products?category_id={category.id}")
    detail = client.get(f"{CATALOG_PREFIX}/products/{product.id}")
    search = client.get(f"{CATALOG_PREFIX}/products/search?query=Old+Widget")

    for answer in (listing, detail, search):
        assert answer.status_code == 200
    assert detail.json()["images"] == []
    assert [item["images"] for item in listing.json()["items"]] == [[]]
    assert [item["id"] for item in search.json()["items"]] == [product.id]


def test_an_update_cannot_empty_the_image_list(client, db_session):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    created = client.post(
        f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
    ).json()

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{created['id']}", json={"images": []}, headers=headers
    )

    assert response.status_code == 422


class _ResponseCannotBeBuilt(Exception):
    pass


def _break_the_response(monkeypatch):
    def broken(*args, **kwargs):
        raise _ResponseCannotBeBuilt

    monkeypatch.setattr("app.api.v1.admin.ProductResponse", broken)


def test_a_create_whose_response_cannot_be_built_stores_nothing(client, db_session, monkeypatch):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    _break_the_response(monkeypatch)

    with pytest.raises(_ResponseCannotBeBuilt):
        client.post(
            f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
        )

    assert db_session.query(Product).count() == 0


def test_an_update_whose_response_cannot_be_built_changes_nothing(client, db_session, monkeypatch):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    created = client.post(
        f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
    ).json()
    _break_the_response(monkeypatch)

    with pytest.raises(_ResponseCannotBeBuilt):
        client.patch(
            f"{ADMIN_PREFIX}/products/{created['id']}", json={"stock_quantity": 99}, headers=headers
        )

    db_session.expire_all()
    assert db_session.get(Product, created["id"]).stock_quantity == created["stock_quantity"]
