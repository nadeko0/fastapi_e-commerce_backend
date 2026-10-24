"""
Shared drivers for the payment-chain tests (test_payment_paths.py,
test_payment_chain.py, test_payment_state_machine.py): a small `Shop` that
sets up catalog and accounts, and drives the real HTTP API - cart, order,
/pay, /checkout-session, /refund, the Stripe webhook, cancellation and the
unpaid-order expiry - on the in-process mock provider, plus an `Outbox`
that records every email the order code queues.

Not a test module itself (no test_ prefix); tests/integration is on
sys.path because pytest's prepend import mode puts each test file's
directory there, which is how the test modules import it.
"""
import json
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from stripe_event_support import deliver

from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.services.payment.stripe_provider import (
    TOKEN_DECLINE,
    TOKEN_REQUIRES_ACTION,
    TOKEN_SUCCESS_DEFAULT,
    StripePaymentProvider,
)

USERS = "/api/v1/users"
CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
WEBHOOK = f"{ORDERS}/webhooks/stripe"

PAY_OK = TOKEN_SUCCESS_DEFAULT
PAY_DECLINED = TOKEN_DECLINE
PAY_3DS = TOKEN_REQUIRES_ACTION

# The order routes' email senders, by the name the routes import them
# under, and the one the expiry task uses.
ORDER_EMAILS = {
    "confirmation": "app.api.v1.orders.send_order_confirmation_email",
    "status_update": "app.api.v1.orders.send_order_status_update_email",
    "cancellation": "app.api.v1.orders.send_order_cancellation_email",
}
TASK_CANCELLATION_EMAIL = "app.tasks.send_order_cancellation_email"


def cents(amount) -> int:
    """Decimal (or its string form) in major units -> integer cents."""
    return int((Decimal(str(amount)) * 100).to_integral_value())


def new_key() -> str:
    return uuid.uuid4().hex


class Outbox:
    """Every email queued by the order code, as (kind, recipient, order id,
    status, payment_status) at the moment it was queued."""

    def __init__(self, monkeypatch):
        self.sent = []
        for kind, target in ORDER_EMAILS.items():
            monkeypatch.setattr(target, self._recorder(kind))
        monkeypatch.setattr(TASK_CANCELLATION_EMAIL, self._recorder("cancellation"))

    def _recorder(self, kind):
        def _record(email, order, *args, **kwargs):
            self.sent.append((kind, email, order.id, _value(order.status),
                              _value(order.payment_status)))
            return True
        return _record

    def take(self):
        """What was queued since the last take(), and forget it."""
        sent, self.sent = self.sent, []
        return sent


def _value(v):
    return v.value if hasattr(v, "value") else v


@dataclass
class Buyer:
    headers: dict
    user_id: int
    email: str
    address_id: int


class Shop:
    """Catalog, accounts and HTTP calls for one test. Every call goes
    through the API; only setup the API has no route for (a category, a
    product, an admin, a verified buyer) is written to the database."""

    def __init__(self, client, db):
        self.client = client
        self.db = db

    # -- Setup -------------------------------------------------------------

    def product(self, price="19.99", stock=10, name="Widget") -> Product:
        category = Category(name=f"cat-{uuid.uuid4().hex[:8]}", path=[], level=0)
        self.db.add(category)
        self.db.commit()
        product = Product(name=name, description="A product", price=Decimal(price),
                          stock_quantity=stock, category_id=category.id,
                          images=["https://img.test/p.png"])
        self.db.add(product)
        self.db.commit()
        self.db.refresh(product)
        return product

    def variant(self, product, price_override=None, stock=10) -> ProductVariant:
        variant = ProductVariant(
            product_id=product.id, sku=f"SKU-{uuid.uuid4().hex[:10]}",
            attributes={"size": "M"},
            price_override=Decimal(price_override) if price_override is not None else None,
            stock_quantity=stock,
        )
        self.db.add(variant)
        self.db.commit()
        self.db.refresh(variant)
        return variant

    def buyer(self, email=None) -> Buyer:
        """A verified customer with a complete profile and one address."""
        email = email or f"buyer-{uuid.uuid4().hex[:10]}@example.com"
        user = User(email=email, hashed_password=get_password_hash("Str0ngPass1"),
                    full_name="Buyer One", phone="+15551234567", role="client",
                    is_active=True, is_email_verified=True, gdpr_consent=True,
                    privacy_policy_accepted=True)
        self.db.add(user)
        self.db.commit()
        address = Address(user_id=user.id, street="1 Main Street", city="Metropolis",
                          state="NY", postal_code="10001", country="US")
        self.db.add(address)
        self.db.commit()
        return Buyer({"Authorization": f"Bearer {create_access_token(user.id)}"},
                     user.id, email, address.id)

    def admin(self) -> dict:
        admin = User(email=f"admin-{uuid.uuid4().hex[:10]}@example.com",
                     hashed_password=get_password_hash("Str0ngPass1"), full_name="Admin",
                     role="admin", is_active=True, is_email_verified=True,
                     gdpr_consent=True, privacy_policy_accepted=True)
        self.db.add(admin)
        self.db.commit()
        return {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    # -- Customer ----------------------------------------------------------

    def add_to_cart(self, buyer: Buyer, product, quantity=1, variant=None):
        params = {"product_id": product.id, "quantity": quantity}
        if variant is not None:
            params["variant_id"] = variant.id
        return self.client.post(f"{CART}/items", json=params, headers=buyer.headers)

    def place_order(self, buyer: Buyer, key=None):
        return self.client.post(ORDERS, json={"shipping_address_id": buyer.address_id},
                                headers={**buyer.headers, "Idempotency-Key": key or new_key()})

    def order_of(self, buyer: Buyer, product, quantity=1) -> dict:
        """Cart one product and place the order; the order as returned."""
        added = self.add_to_cart(buyer, product, quantity)
        assert added.status_code == 200, added.text
        response = self.place_order(buyer)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def pay(self, buyer: Buyer, order_id, token=PAY_OK, amount=None, currency=None, key=None):
        if amount is None:
            amount = str(self.order(order_id).total_amount)
        body = {"order_id": order_id, "payment_method": "stripe", "amount": str(amount),
                "payment_method_token": token}
        if currency is not None:
            body["currency"] = currency
        return self.client.post(f"{ORDERS}/{order_id}/pay", json=body,
                                headers={**buyer.headers, "Idempotency-Key": key or new_key()})

    def checkout_session(self, buyer: Buyer, order_id, key=None, payload=None):
        return self.client.post(f"{ORDERS}/{order_id}/checkout-session", json=payload,
                                headers={**buyer.headers, "Idempotency-Key": key or new_key()})

    def cancel(self, buyer: Buyer, order_id):
        return self.client.post(f"{ORDERS}/{order_id}/cancel", headers=buyer.headers)

    def get_order(self, buyer: Buyer, order_id):
        return self.client.get(f"{ORDERS}/{order_id}", headers=buyer.headers)

    def list_orders(self, buyer: Buyer):
        return self.client.get(f"{USERS}/orders", headers=buyer.headers)

    # -- Admin -------------------------------------------------------------

    def set_status(self, admin_headers, order_id, status):
        return self.client.put(f"{ORDERS}/{order_id}/status", json={"status": status},
                               headers=admin_headers)

    def refund(self, admin_headers, order_id, amount=None, key=None):
        body = {} if amount is None else {"amount": str(amount)}
        return self.client.post(f"{ORDERS}/{order_id}/refund", json=body,
                                headers={**admin_headers, "Idempotency-Key": key or new_key()})

    def expire_unpaid(self, *order_ids):
        """Back-date the orders past UNPAID_ORDER_EXPIRE_MINUTES and run the
        expiry task (needs the tasks_db fixture)."""
        from app import tasks

        for order_id in order_ids:
            self.db.get(Order, order_id).created_at = utc_now() - timedelta(
                minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)
        self.db.commit()
        return tasks.cancel_expired_unpaid_orders()

    # -- Stripe ------------------------------------------------------------

    def _post_event(self, event_type, obj, event_id=None, timestamp=None):
        body = json.dumps({
            "id": event_id or f"evt_{uuid.uuid4().hex[:24]}",
            "object": "event",
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": obj},
        }).encode()
        signature = StripePaymentProvider.sign_payload(
            body, settings.STRIPE_WEBHOOK_SECRET.get_secret_value(), timestamp=timestamp)
        response = self.client.post(WEBHOOK, content=body,
                                    headers={"Stripe-Signature": signature,
                                             "Content-Type": "application/json"})
        return response, body

    def webhook_raw(self, event_type, obj, event_id=None, timestamp=None):
        """Deliver an event to the route and nothing more: the route's own
        answer, the event stored and its task message published, not yet
        applied."""
        return self._post_event(event_type, obj, event_id, timestamp)[0]

    def webhook(self, event_type, obj, event_id=None, timestamp=None):
        """Deliver an event: the route stores it, then the worker applies it.
        The answer carries what applying it produced (see Delivered)."""
        return deliver(*self._post_event(event_type, obj, event_id, timestamp))

    def intent_event(self, event_type, payment: Payment, amount=None, currency=None,
                     event_id=None, **extra):
        """A payment_intent.* event for the attempt, reporting by default
        exactly what the attempt charges. The same event_id twice is a
        redelivery of one event."""
        obj = {
            "id": payment.payment_intent_id, "object": "payment_intent",
            "amount": payment.amount if amount is None else amount,
            "amount_received": payment.amount if amount is None else amount,
            "currency": (currency or payment.currency).lower(),
            "metadata": {"order_id": str(payment.order_id)},
            **extra,
        }
        return self.webhook(event_type, obj, event_id=event_id)

    def session_event(self, event_type, payment: Payment, payment_status="paid",
                      payment_intent=None, amount=None, currency=None):
        obj = {
            "id": payment.checkout_session_id, "object": "checkout.session",
            "payment_status": payment_status,
            "amount_total": payment.amount if amount is None else amount,
            "currency": (currency or payment.currency).lower(),
            "metadata": {"order_id": str(payment.order_id)},
        }
        if payment_intent is not None:
            obj["payment_intent"] = payment_intent
        return self.webhook(event_type, obj)

    # -- Reading state -----------------------------------------------------

    def order(self, order_id) -> Order:
        self.db.expire_all()
        return self.db.get(Order, order_id)

    def payments(self, order_id):
        self.db.expire_all()
        return (self.db.query(Payment).filter(Payment.order_id == order_id)
                .order_by(Payment.id).all())

    def stock(self, item) -> int:
        self.db.expire_all()
        return self.db.get(type(item), item.id).stock_quantity

    def webhook_event_ids(self):
        self.db.expire_all()
        return {e.event_id for e in self.db.query(WebhookEvent).all()}


def payment_rows(shop: Shop, order_id):
    """The order's attempts as comparable tuples:
    (status, amount, amount_refunded, currency, requires_manual_review)."""
    return [(p.status, p.amount, p.amount_refunded, p.currency, p.requires_manual_review)
            for p in shop.payments(order_id)]


def order_state(shop: Shop, order_id):
    order = shop.order(order_id)
    return _value(order.status), _value(order.payment_status)


def reset_provider() -> None:
    StripePaymentProvider.reset_mock_state()


def latest_payment(shop: Shop, order_id) -> Payment | None:
    payments = shop.payments(order_id)
    return payments[-1] if payments else None
