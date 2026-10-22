"""Product responses must serialize without Pydantic serializer warnings.

`images` is validated as a URL but handled as the plain string everywhere
(string-array column, JSON response). A str in a field whose schema says
HttpUrl made Pydantic warn on every dump; the warning is turned into an
error here, so it cannot come back unnoticed.
"""
import pytest
from test_admin import (
    ADMIN_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)
from test_products import CATALOG_PREFIX, _make_product

from app.schemas.product import ProductCreate, ProductUpdate

pytestmark = pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")

URL = "https://example.com/widget.png"


def test_schemas_dump_the_same_url_strings_without_warnings():
    created = ProductCreate(**_valid_product_payload(1))
    assert created.model_dump()["images"] == [URL]
    assert '"images":["https://example.com/widget.png"]' in created.model_dump_json()

    update = ProductUpdate(images=[URL, URL])
    assert update.model_dump(exclude_unset=True)["images"] == [URL]


def test_catalog_endpoints_return_images_without_warnings(client, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, name="Gadget")

    detail = client.get(f"{CATALOG_PREFIX}/products/{product.id}")
    listing = client.get(f"{CATALOG_PREFIX}/products")
    search = client.get(f"{CATALOG_PREFIX}/products/search", params={"query": "Gadget"})

    for response in (detail, listing, search):
        assert response.status_code == 200
    assert detail.json()["data"]["images"] == [URL]
    assert listing.json()["data"]["items"][0]["images"] == [URL]
    assert search.json()["data"]["items"][0]["images"] == [URL]


def test_admin_create_and_update_return_images_without_warnings(client, db_session):
    _make_admin(db_session)
    headers = _auth_headers(_login_as(client, "admin@example.com", "AdminPass1"))
    category = _make_category(db_session)

    created = client.post(
        f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
    )
    assert created.status_code in (200, 201)
    product_id = created.json()["data"]["id"]
    assert created.json()["data"]["images"] == [URL]

    updated = client.put(
        f"{ADMIN_PREFIX}/products/{product_id}",
        json={"images": ["https://example.com/b.png"]},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["images"] == ["https://example.com/b.png"]
