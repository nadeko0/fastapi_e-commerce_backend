import json
from datetime import timedelta

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.user import User


def _erased_user_with_order(db):
    when = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 2)
    user = User(email="jane@example.com", hashed_password=get_password_hash("Str0ngPass1"),
                full_name="Jane Roe", is_active=False, data_deletion_requested=True,
                data_deletion_date=when)
    db.add(user); db.commit()
    addr = Address(user_id=user.id, street="Main 1", city="Berlin", state="BE",
                   postal_code="10115", country="DE", is_active=True)
    db.add(addr); db.commit()
    order = Order(user_id=user.id, shipping_address_id=addr.id, total_amount=10)
    db.add(order); db.commit()
    db.add(Payment(order_id=order.id, payment_intent_id="pi_late", amount=1000, currency="eur"))
    db.commit()
    return user, order


def _store(db, event_id, event_type, obj):
    from app.services import stripe_events
    payload = json.dumps({"id": event_id, "type": event_type, "created": 1, "data": {"object": obj}})
    stripe_events.store_event(db, event_id, event_type, payload)


def test_2c01_event_arriving_after_erasure_keeps_pii_forever(db_session):
    user, order = _erased_user_with_order(db_session)
    assert tasks.purge_expired_deletion_requests(db_session) == 1
    # A dispute / refund arrives weeks later, carrying billing details.
    _store(db_session, "evt_after", "charge.refunded", {
        "id": "ch_1", "object": "charge", "payment_intent": "pi_late",
        "billing_details": {"name": "Jane Roe", "email": "jane@example.com"},
        "metadata": {"order_id": str(order.id)}, "refunds": {"data": []},
    })
    # Every later run of both purges skips this user (already anonymised).
    assert tasks.purge_expired_deletion_requests(db_session) == 0
    assert tasks.purge_expired_retention(db_session) == 0
    db_session.expire_all()
    row = db_session.query(WebhookEvent).filter_by(event_id="evt_after").one()
    assert "jane@example.com" in row.payload  # PII retained indefinitely


def test_2c05_welcome_email_crashes_without_full_name():
    import pytest
    from app.services.email import send_welcome_email
    with pytest.raises(AttributeError):
        send_welcome_email("a@example.com", None, "tok")


def test_2c07_patch_me_changes_marketing_consent_without_history(client, db_session):
    client.post("/api/v1/users/register", json={
        "email": "c@example.com", "password": "Zx9!qwerty-unique", "gdpr_consent": True,
        "privacy_policy_accepted": True, "marketing_consent": False})
    tok = client.post("/api/v1/users/login", data={"username": "c@example.com",
                                                   "password": "Zx9!qwerty-unique"}).json()["access_token"]
    r = client.patch("/api/v1/users/me", json={"marketing_consent": True},
                     headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    db_session.expire_all()
    u = db_session.query(User).filter_by(email="c@example.com").one()
    assert u.marketing_consent is True
    assert u.marketing_consent_date is None  # no date
    assert [e for e in u.consent_history if e["type"] == "marketing"][-1]["granted"] is False


def test_2c08_cors_preflight_rejection_is_text_plain(client):
    r = client.options("/api/v1/products", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")


def test_2c06_address_edit_rewrites_shipped_order(client, db_session):
    client.post("/api/v1/users/register", json={
        "email": "d@example.com", "password": "Zx9!qwerty-unique", "gdpr_consent": True,
        "privacy_policy_accepted": True})
    tok = client.post("/api/v1/users/login", data={"username": "d@example.com",
                                                   "password": "Zx9!qwerty-unique"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    a = client.post("/api/v1/users/addresses", headers=h, json={
        "street": "Main 1 st", "city": "Berlin", "state": "BE", "postal_code": "10115",
        "country": "DE"}).json()
    u = db_session.query(User).filter_by(email="d@example.com").one()
    o = Order(user_id=u.id, shipping_address_id=a["id"], total_amount=10,
              status="delivered", payment_status="paid")
    db_session.add(o); db_session.commit()
    r = client.patch(f"/api/v1/users/addresses/{a['id']}", headers=h, json={"street": "Elsewhere 99", "country": "US"})
    assert r.status_code == 200
    got = client.get(f"/api/v1/orders/{o.id}", headers=h).json()
    assert got["shipping_address"]["street"] == "Elsewhere 99"
