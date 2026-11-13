"""
An order's shipping_address is the copy taken at checkout: changes to the
customer's address book after the order was placed do not show through it,
including the fields the copy does not hold (is_default, created_at,
updated_at), which used to be read from the live address row.
"""
import pytest
from payment_support import USERS, Shop, reset_provider


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


def test_switching_the_default_address_does_not_change_a_placed_order(client, db_session):
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    made_default = client.post(f"{USERS}/addresses/default", json={"address_id": buyer.address_id},
                               headers=buyer.headers)
    assert made_default.status_code == 200, made_default.text
    order = shop.order_of(buyer, shop.product())
    placed = shop.get_order(buyer, order["id"]).json()["shipping_address"]

    # Another address becomes the default; the one the order used is edited too.
    added = client.post(f"{USERS}/addresses", headers=buyer.headers, json={
        "street": "2 Side Street", "city": "Gotham", "state": "NJ", "postal_code": "07001",
        "country": "US", "is_default": True,
    })
    assert added.status_code == 201, added.text
    edited = client.patch(f"{USERS}/addresses/{buyer.address_id}", headers=buyer.headers,
                          json={"delivery_instructions": "Leave at the door"})
    assert edited.status_code == 200, edited.text

    shown = shop.get_order(buyer, order["id"]).json()["shipping_address"]
    assert shown == placed
    assert shown["is_default"] is False
    assert shown["created_at"] == shown["updated_at"] == order["created_at"]
    (listed,) = [o for o in shop.list_orders(buyer).json()["items"] if o["id"] == order["id"]]
    assert listed["shipping_address"] == placed
