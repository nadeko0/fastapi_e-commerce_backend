"""
Integration tests covering GDPR erasure, webhook redaction, and sweep branches
required for the 100 percent line and branch payment coverage gate:
- app/api/v1/orders.py: anonymised-recipient guards on status update and cancel
- app/services/email/messages.py: GDPR deletion completed mail with no retained records
- app/services/stripe_events.py: _redact_if_owner_erased edge cases and branch coverage
- app/tasks.py: snapshot scrub batching, deletion-completed mail failure, sweep anonymised guard
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.email.messages import send_gdpr_deletion_completed
from app.services.stripe_events import _redact_if_owner_erased


def _make_verified_user(db_session, email="buyer@example.com"):
    user = User(
        email=email,
        hashed_password=get_password_hash("Str0ngPass1"),
        full_name="Buyer Person",
        phone="+12025550123",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        is_active=True,
        is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_admin(db_session, email="admin@example.com"):
    admin = User(
        email=email,
        hashed_password=get_password_hash("AdminPass1"),
        full_name="Admin User",
        phone="+123456789",
        role=UserRole.ADMIN,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def _make_category(db_session):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, stock=10, price=Decimal("9.99")):
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category_id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _make_order_with_item(
    db_session, user, product, quantity=1, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
):
    address = Address(
        user_id=user.id,
        street="123 Main Street",
        city="Springfield",
        state="Illinois",
        postal_code="62701",
        country="US",
        is_default=True,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    status_val = status.value if hasattr(status, "value") else str(status)
    payment_val = payment_status.value if hasattr(payment_status, "value") else str(payment_status)

    order = Order(
        user_id=user.id,
        status=status_val,
        payment_status=payment_val,
        shipping_address_id=address.id,
        total_amount=product.price * quantity,
        currency=settings.STORE_CURRENCY,
        ship_to_street=address.street,
        ship_to_city=address.city,
        ship_to_state=address.state,
        ship_to_postal_code=address.postal_code,
        ship_to_country=address.country,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        price_at_time=product.price,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(order)
    return order


# --- app/api/v1/orders.py: branch 575->582 -----------------------------------


def test_admin_status_update_anonymised_recipient_skips_email(client, db_session, monkeypatch):
    """
    Branch 575->582: when updating the status of an order whose owner has
    an @deleted.invalid address, no status update email is sent and the route
    answers normally.
    """
    admin = _make_admin(db_session, email="admin-gdpr-status@example.com")
    admin_token = create_access_token(admin.id)

    owner = _make_verified_user(db_session, email="deleted-user-575@deleted.invalid")
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order = _make_order_with_item(
        db_session, owner, product, quantity=1, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
    )

    sent = []
    monkeypatch.setattr(
        "app.api.v1.orders.send_order_status_update_email",
        lambda *args, **kwargs: sent.append(args),
    )

    response = client.put(
        f"/api/v1/orders/{order.id}/status",
        json={"status": "cancelled"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert sent == []


# --- app/api/v1/orders.py: branch 642->647 -----------------------------------


def test_cancel_own_order_anonymised_recipient_skips_email(client, db_session, monkeypatch):
    """
    Branch 642->647: when a customer whose account has an @deleted.invalid
    address cancels their own unpaid order, no cancellation mail goes out
    and the route still answers normally.
    """
    owner = _make_verified_user(db_session, email="deleted-user-642@deleted.invalid")
    token = create_access_token(owner.id)

    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order = _make_order_with_item(
        db_session, owner, product, quantity=1, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
    )

    sent = []
    monkeypatch.setattr(
        "app.api.v1.orders.send_order_cancellation_email",
        lambda *args, **kwargs: sent.append(args),
    )

    response = client.post(
        f"/api/v1/orders/{order.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert sent == []


# --- app/services/email/messages.py: branch 418->422 ------------------------


def test_send_gdpr_deletion_completed_no_retained_records(monkeypatch):
    """
    Branch 418->422: send_gdpr_deletion_completed when called with no
    retained records (retained_records=None) sends the email without
    a 'Retained records' section.
    """
    outbox = []

    class DummyEmailProvider:
        def send(self, to_email, subject, html_content):
            outbox.append((to_email, subject, html_content))
            return True

    monkeypatch.setattr("app.services.email.get_email_provider", lambda: DummyEmailProvider())

    result = send_gdpr_deletion_completed("erased@example.com", retained_records=None)
    assert result is True
    assert len(outbox) == 1
    to_email, subject, html_content = outbox[0]
    assert to_email == "erased@example.com"
    assert "Your Personal Data Has Been Deleted" in subject
    assert "Retained records" not in html_content


# --- app/services/stripe_events.py: lines 675-678, branch 690->693, line 694 -


def test_redact_if_owner_erased_invalid_json_left_alone(db_session):
    """Lines 675-676: a payload that is not valid JSON is left alone."""
    row = WebhookEvent(
        event_id="evt_invalid_json",
        event_type="payment_intent.succeeded",
        payload="not-json-{",
    )
    _redact_if_owner_erased(db_session, row)
    assert row.payload == "not-json-{"


def test_redact_if_owner_erased_non_dict_payload_left_alone(db_session):
    """Lines 677-678: a payload that is not an object is left alone."""
    payload_str = json.dumps(["an", "array"])
    row = WebhookEvent(
        event_id="evt_array",
        event_type="payment_intent.succeeded",
        payload=payload_str,
    )
    _redact_if_owner_erased(db_session, row)
    assert row.payload == payload_str


def test_redact_if_owner_erased_already_redacted_left_alone(db_session):
    """Lines 677-678: an already redacted payload is left alone."""
    payload = {"id": "evt_redacted", "redacted": True, "data": {"object": {"id": "pi_1"}}}
    row = WebhookEvent(
        event_id="evt_redacted",
        event_type="payment_intent.succeeded",
        payload=json.dumps(payload),
    )
    _redact_if_owner_erased(db_session, row)
    assert row.payload == json.dumps(payload)


def test_redact_if_owner_erased_order_id_beyond_int32_causes_no_error(db_session):
    """Line 684: an order id beyond the int32 range in metadata causes no error."""
    payload = {
        "id": "evt_overflow",
        "data": {
            "object": {
                "id": "pi_overflow",
                "metadata": {"order_id": str(2**31 + 100)},
            }
        },
    }
    row = WebhookEvent(
        event_id="evt_overflow",
        event_type="payment_intent.succeeded",
        payload=json.dumps(payload),
    )
    _redact_if_owner_erased(db_session, row)
    assert json.loads(row.payload).get("redacted") is None


def test_redact_if_owner_erased_no_references_left_alone(db_session):
    """Line 694: an event with no references is left alone."""
    payload = {"id": "evt_no_ref", "data": {"object": {}}}
    row = WebhookEvent(
        event_id="evt_no_ref",
        event_type="custom.event",
        payload=json.dumps(payload),
    )
    _redact_if_owner_erased(db_session, row)
    assert row.payload == json.dumps(payload)


def test_redact_if_owner_erased_branch_690_to_693_order_id_only(db_session):
    """
    Branch 690->693: provider_ids is empty, order_ids is present.
    If the account requested deletion, payload is redacted.
    """
    user = _make_verified_user(db_session, email="user-branch690@example.com")
    user.data_deletion_requested = True
    db_session.commit()

    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order = _make_order_with_item(
        db_session, user, product, quantity=1, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
    )

    payload = {
        "id": "evt_order_only",
        "data": {
            "object": {
                "metadata": {"order_id": str(order.id)},
            }
        },
    }
    row = WebhookEvent(
        event_id="evt_order_only",
        event_type="order.updated",
        payload=json.dumps(payload),
    )
    _redact_if_owner_erased(db_session, row)
    assert json.loads(row.payload).get("redacted") is True


def test_redact_if_owner_erased_account_without_deletion_kept_unredacted(db_session):
    """An event about an account without a deletion request is kept unredacted."""
    user = _make_verified_user(db_session, email="user-nodelete@example.com")
    user.data_deletion_requested = False
    db_session.commit()

    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order = _make_order_with_item(
        db_session, user, product, quantity=1, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
    )

    payment = Payment(
        order_id=order.id,
        payment_intent_id="pi_nodelete",
        amount=1000,
        currency="USD",
        status="succeeded",
        idempotency_key="idem_nodelete",
    )
    db_session.add(payment)
    db_session.commit()

    payload = {
        "id": "evt_nodelete",
        "data": {
            "object": {
                "id": "pi_nodelete",
                "metadata": {"order_id": str(order.id)},
            }
        },
    }
    row = WebhookEvent(
        event_id="evt_nodelete",
        event_type="payment_intent.succeeded",
        payload=json.dumps(payload),
    )
    _redact_if_owner_erased(db_session, row)
    assert json.loads(row.payload).get("redacted") is None


# --- app/tasks.py: branch 316->297, lines 355-357, branch 538->484 -----------


def test_snapshot_scrub_batching_loop_once_more_when_batch_size_equals_eligible(db_session):
    """
    Branch 316->297: in the snapshot scrub's batching, a batch size equal
    to the number of eligible rows makes len(ids) < batch_size False, so the
    loop runs once more and finds nothing.
    """
    owner = _make_verified_user(db_session, email="deleted-user-scrub@deleted.invalid")
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order = _make_order_with_item(
        db_session, owner, product, quantity=1, status=OrderStatus.DELIVERED, payment_status=PaymentStatus.PAID
    )
    order.ship_to_street = "456 Snapshot Street"
    order.ship_to_city = "Springfield"
    order.ship_to_state = "IL"
    order.ship_to_postal_code = "62701"
    db_session.commit()

    scrubbed = tasks._scrub_retained_order_snapshots(db_session, batch_size=1)
    assert scrubbed == 1

    db_session.refresh(order)
    assert order.ship_to_street == "REDACTED"
    assert order.ship_to_city == "REDACTED"
    assert order.ship_to_state == "REDACTED"
    assert order.ship_to_postal_code == "REDACTED"


def test_purge_deletion_completed_mail_failure_erasure_stands_and_logs(
    db_session, monkeypatch, caplog
):
    """
    Lines 355-357: when the deletion-completed mail raises, the erasure stands,
    later accounts are still handled, and the log line does not contain the address.
    """
    past_date = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 10)
    user1 = _make_verified_user(db_session, email="fail_mail_user1@example.com")
    user1.is_active = False
    user1.data_deletion_requested = True
    user1.data_deletion_date = past_date

    user2 = _make_verified_user(db_session, email="succeed_mail_user2@example.com")
    user2.is_active = False
    user2.data_deletion_requested = True
    user2.data_deletion_date = past_date
    db_session.commit()

    def mock_send(recipient, retained=None):
        if recipient == "fail_mail_user1@example.com":
            raise RuntimeError("SMTP network down")
        return True

    monkeypatch.setattr("app.tasks.send_gdpr_deletion_completed", mock_send)

    with caplog.at_level(logging.ERROR):
        purged = tasks.purge_expired_deletion_requests(db_session, batch_size=10)

    assert purged == 2

    db_session.refresh(user1)
    db_session.refresh(user2)
    assert user1.email == f"deleted-user-{user1.id}@deleted.invalid"
    assert user2.email == f"deleted-user-{user2.id}@deleted.invalid"
    assert user1.full_name is None
    assert user2.full_name is None

    assert "Could not send a deletion completed email" in caplog.text
    assert "fail_mail_user1@example.com" not in caplog.text
    assert "succeed_mail_user2@example.com" not in caplog.text


def test_expire_unpaid_orders_anonymised_recipient_skips_email_and_cancels(
    db_session, monkeypatch
):
    """
    Branch 538->484: the sweep's anonymised-recipient guard: when the order
    owner has an @deleted.invalid address, no cancellation mail goes out
    and the order is cancelled.
    """
    owner = _make_verified_user(db_session, email="deleted-user-sweep484@deleted.invalid")
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=8)
    order = _make_order_with_item(
        db_session, owner, product, quantity=2, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING
    )
    order.created_at = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 15)
    db_session.commit()

    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_cancellation_email",
        lambda *args, **kwargs: sent.append(args),
    )

    cancelled = tasks.expire_unpaid_orders(db_session)
    assert cancelled == 1
    assert sent == []

    db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    db_session.refresh(product)
    assert product.stock_quantity == 10  # stock restored
