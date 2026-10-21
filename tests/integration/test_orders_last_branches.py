"""
The last branches of app/api/v1/orders.py that no other test reached.
"""
import pytest
from payment_support import Outbox, Shop, order_state, payment_rows, reset_provider
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.payment import Payment


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


def test_pay_whose_attempt_row_is_refused_by_the_database_for_another_reason_is_not_swallowed(
    shop, outbox, monkeypatch
):
    """
    The IntegrityError handler in /pay only absorbs the idempotency-key
    race, where the twin's row is there to be returned. A database refusal
    of the attempt with no such row behind it must surface as a server error, not
    be answered with a payment that does not exist, and must leave nothing recorded.
    """
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    real_commit = Session.commit

    def _refuse_the_attempt(self):
        if any(isinstance(row, Payment) for row in self.new):
            raise IntegrityError("INSERT INTO payments", {}, Exception("refused"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", _refuse_the_attempt)

    response = shop.pay(buyer, order["id"])

    monkeypatch.setattr(Session, "commit", real_commit)
    assert response.status_code == 500
    assert payment_rows(shop, order["id"]) == []
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert outbox.take() == []
