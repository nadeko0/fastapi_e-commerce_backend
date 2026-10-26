"""Audit 2B reproductions (scratch; not part of the suite)."""
from test_admin import (
    ADMIN_PREFIX, CATALOG_PREFIX, _auth_headers, _login_as, _make_admin, _make_category,
    _valid_product_payload,
)

MP = {"Content-Type": "application/merge-patch+json"}


def _admin(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def _char(name, value):
    return {"name": name, "value": value, "unit": None, "filterable": True}


def test_merge_patch_nested_object_is_replaced_not_merged(client, db_session):
    h = _admin(client, db_session)
    cat = _make_category(db_session)
    body = _valid_product_payload(cat.id)
    body["characteristics"] = {"color": _char("Color", "red"), "size": _char("Size", "M")}
    p = client.post(f"{ADMIN_PREFIX}/products", json=body, headers=h).json()
    r = client.patch(f"{ADMIN_PREFIX}/products/{p['id']}",
                     json={"characteristics": {"color": _char("Color", "blue")}}, headers={**h, **MP})
    assert r.status_code == 200
    print("after nested patch:", sorted(r.json()["characteristics"]))
    assert "size" not in r.json()["characteristics"]
    r2 = client.patch(f"{ADMIN_PREFIX}/products/{p['id']}",
                      json={"characteristics": {"color": None}}, headers={**h, **MP})
    print("null member status:", r2.status_code)
    assert r2.status_code == 422


def test_merge_patch_variant_attributes_null_is_stored(client, db_session):
    h = _admin(client, db_session)
    cat = _make_category(db_session)
    p = client.post(f"{ADMIN_PREFIX}/products", json=_valid_product_payload(cat.id), headers=h).json()
    v = client.post(f"{ADMIN_PREFIX}/products/{p['id']}/variants",
                    json={"sku": "SKU-1", "attributes": {"size": "M", "color": "red"}, "stock_quantity": 3},
                    headers=h).json()
    r = client.patch(f"{ADMIN_PREFIX}/products/{p['id']}/variants/{v['id']}",
                     json={"attributes": {"size": None}}, headers={**h, **MP})
    print("variant attributes after patch:", r.status_code, r.json().get("attributes"))
    assert r.status_code == 200
    assert r.json()["attributes"] == {"size": None}


def test_plain_line_allowed_for_product_with_variants(client, db_session):
    h = _admin(client, db_session)
    cat = _make_category(db_session)
    p = client.post(f"{ADMIN_PREFIX}/products", json=_valid_product_payload(cat.id), headers=h).json()
    v = client.post(f"{ADMIN_PREFIX}/products/{p['id']}/variants",
                    json={"sku": "SKU-2", "stock_quantity": 3}, headers=h).json()
    r1 = client.post(f"{CATALOG_PREFIX}/cart/items", json={"product_id": p["id"], "quantity": 1}, headers=h)
    r2 = client.post(f"{CATALOG_PREFIX}/cart/items",
                     json={"product_id": p["id"], "variant_id": v["id"], "quantity": 1}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(r2.json()["items"]) == 2
