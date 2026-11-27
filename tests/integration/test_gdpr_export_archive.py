"""Tests for GDPR data export archive building, email notification, and cleanup."""

import json
import os
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment
from app.models.payment_ledger import PaymentRefund
from app.models.product import Product
from app.models.user import User
from app.services.gdpr import (
    _ADDRESS_FIELDS,
    _ITEM_FIELDS,
    _ORDER_FIELDS,
    _PAYMENT_FIELDS,
    _REFUND_FIELDS,
    _USER_FIELDS,
)
from app.services.gdpr_export import delete_expired_exports, export_path


class _Mailbox:
    """Capture provider sends in memory for verification."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to_email: str, subject: str, html_content: str) -> bool:
        self.sent.append((to_email, subject, html_content))
        return True


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> _Mailbox:
    box = _Mailbox()
    monkeypatch.setattr("app.services.email.get_email_provider", lambda: box)
    return box


def _create_category(db_session) -> Category:
    cat = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(cat)
    db_session.commit()
    db_session.refresh(cat)
    return cat


def _create_product(db_session, category_id: int, name: str, price: str) -> Product:
    prod = Product(
        name=name,
        description=f"Description for {name}",
        price=Decimal(price),
        stock_quantity=10,
        category_id=category_id,
        images=[],
        characteristics={},
    )
    db_session.add(prod)
    db_session.commit()
    db_session.refresh(prod)
    return prod


def _create_user(db_session, email: str, export_requests: list | None = None) -> User:
    user = User(
        email=email,
        hashed_password=get_password_hash("ValidPass123!"),
        full_name="Test User",
        phone="+1234567890",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        data_export_requests=export_requests if export_requests is not None else [],
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_build_gdpr_export_full_success(
    tasks_db, db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mailbox: _Mailbox
) -> None:
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)

    cat = _create_category(db_session)
    prod1 = _create_product(db_session, cat.id, "Prod 1", "20.00")
    prod2 = _create_product(db_session, cat.id, "Prod 2", "15.00")

    req_id = "export_0123456789abcdef0123456789abcdef"
    user1 = _create_user(
        db_session,
        "alice@example.com",
        export_requests=[{
            "request_id": req_id,
            "requested_at": utc_now().isoformat(),
            "status": "building",
            "expires_at": None,
        }],
    )

    addr1 = Address(
        user_id=user1.id,
        street="10 Downing St",
        city="London",
        state="Greater London",
        postal_code="SW1A 2AA",
        country="GB",
        is_active=True,
    )
    db_session.add(addr1)
    db_session.commit()
    db_session.refresh(addr1)

    order1 = Order(
        user_id=user1.id,
        shipping_address_id=addr1.id,
        total_amount=Decimal("50.00"),
        currency="USD",
        status="delivered",
        payment_status="paid",
    )
    db_session.add(order1)
    db_session.commit()
    db_session.refresh(order1)

    item1 = OrderItem(
        order_id=order1.id,
        product_id=prod1.id,
        quantity=1,
        price_at_time=Decimal("20.00"),
    )
    item2 = OrderItem(
        order_id=order1.id,
        product_id=prod2.id,
        quantity=2,
        price_at_time=Decimal("15.00"),
    )
    db_session.add_all([item1, item2])
    db_session.commit()

    payment1 = Payment(
        order_id=order1.id,
        payment_intent_id="pi_alice_1",
        amount=5000,
        currency="USD",
        status="succeeded",
    )
    db_session.add(payment1)
    db_session.commit()
    db_session.refresh(payment1)

    refund1 = PaymentRefund(
        payment_id=payment1.id,
        refund_id="re_alice_1",
        amount=1500,
        currency="USD",
        status="succeeded",
        source="admin",
    )
    db_session.add(refund1)
    db_session.commit()

    # Second user with order to ensure strict isolation
    user2 = _create_user(db_session, "bob@example.com")
    addr2 = Address(
        user_id=user2.id,
        street="221B Baker St",
        city="London",
        state="Greater London",
        postal_code="NW1 6XE",
        country="GB",
        is_active=True,
    )
    db_session.add(addr2)
    db_session.commit()
    db_session.refresh(addr2)

    order2 = Order(
        user_id=user2.id,
        shipping_address_id=addr2.id,
        total_amount=Decimal("30.00"),
        currency="USD",
        status="delivered",
        payment_status="paid",
    )
    db_session.add(order2)
    db_session.commit()
    db_session.refresh(order2)

    item3 = OrderItem(
        order_id=order2.id,
        product_id=prod1.id,
        quantity=1,
        price_at_time=Decimal("30.00"),
    )
    db_session.add(item3)
    db_session.commit()

    # Run the task directly
    result = tasks.build_gdpr_export(user1.id, req_id)
    assert result == "ready"

    zip_file = export_path(req_id)
    assert zip_file.exists()
    assert not zip_file.with_suffix(".zip.tmp").exists()

    with zipfile.ZipFile(zip_file, "r") as zf:
        names = sorted(zf.namelist())
        assert names == ["account.json", "addresses.json", "orders.json", "payments.json", "refunds.json"]

        account = json.loads(zf.read("account.json"))
        assert account["id"] == user1.id
        assert account["email"] == "alice@example.com"
        assert set(account.keys()) == set(_USER_FIELDS)

        addresses = json.loads(zf.read("addresses.json"))
        assert len(addresses) == 1
        assert addresses[0]["id"] == addr1.id
        assert set(addresses[0].keys()) == set(_ADDRESS_FIELDS)

        orders = json.loads(zf.read("orders.json"))
        assert len(orders) == 1
        assert orders[0]["id"] == order1.id
        assert {k for k in orders[0] if k != "items"} == set(_ORDER_FIELDS)
        assert len(orders[0]["items"]) == 2
        assert set(orders[0]["items"][0].keys()) == set(_ITEM_FIELDS)

        payments = json.loads(zf.read("payments.json"))
        assert len(payments) == 1
        assert payments[0]["id"] == payment1.id
        assert set(payments[0].keys()) == set(_PAYMENT_FIELDS)

        refunds = json.loads(zf.read("refunds.json"))
        assert len(refunds) == 1
        assert refunds[0]["payment_id"] == payment1.id
        assert set(refunds[0].keys()) == set(_REFUND_FIELDS)

    db_session.expire_all()
    user_row = db_session.get(User, user1.id)
    req_entry = next(e for e in user_row.data_export_requests if e.get("request_id") == req_id)
    assert req_entry["status"] == "ready"
    assert req_entry["expires_at"] is not None

    assert len(mailbox.sent) == 1
    to_email, subject, body = mailbox.sent[0]
    assert to_email == "alice@example.com"
    assert subject == "Your personal data export is ready"
    assert f"{settings.FRONTEND_URL}/account/data-export/{req_id}" in body
    assert "Article 15" in body
    assert settings.DPO_EMAIL in body
    assert "Prod 1" not in body
    assert "pi_alice_1" not in body
    assert "50.00" not in body


def test_build_gdpr_export_build_fails(
    tasks_db, db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mailbox: _Mailbox
) -> None:
    # Point GDPR_EXPORT_DIR at an existing regular file so mkdir/path creation fails
    bad_dir = tmp_path / "regular_file_blocking_dir"
    bad_dir.write_text("blocked")
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", bad_dir)

    req_id = "export_fail_0123456789abcdef012345"
    user = _create_user(
        db_session,
        "fail@example.com",
        export_requests=[{
            "request_id": req_id,
            "requested_at": utc_now().isoformat(),
            "status": "building",
            "expires_at": None,
        }],
    )

    result = tasks.build_gdpr_export(user.id, req_id)
    assert result == "failed"

    db_session.expire_all()
    user_row = db_session.get(User, user.id)
    entry = next(e for e in user_row.data_export_requests if e.get("request_id") == req_id)
    assert entry["status"] == "failed"

    assert len(mailbox.sent) == 0
    assert not (bad_dir / f"{req_id}.zip").exists()
    assert not (bad_dir / f"{req_id}.zip.tmp").exists()


def test_build_gdpr_export_missing_user_or_entry(
    tasks_db, db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)

    # 1. Nonexistent user
    res1 = tasks.build_gdpr_export(999999, "export_missing_user")
    assert res1 == "gone"

    # 2. User exists, but request_id not in data_export_requests
    user = _create_user(db_session, "user_no_req@example.com", export_requests=[])
    res2 = tasks.build_gdpr_export(user.id, "export_not_found")
    assert res2 == "gone"

    # 3. User exists, but entry lacks "status" (old history)
    user2 = _create_user(
        db_session,
        "old_history@example.com",
        export_requests=[{"request_id": "old_req", "requested_at": utc_now().isoformat()}],
    )
    res3 = tasks.build_gdpr_export(user2.id, "old_req")
    assert res3 == "gone"


def test_build_gdpr_export_user_deleted_during_export(
    tasks_db, db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)

    req_id = "export_deleted_during"
    user = _create_user(
        db_session,
        "deleted_during@example.com",
        export_requests=[{
            "request_id": req_id,
            "requested_at": utc_now().isoformat(),
            "status": "building",
            "expires_at": None,
        }],
    )

    from app.services import gdpr_export
    original_build = gdpr_export.build_export

    def build_and_delete_user(db, user_id, request_id):
        original_build(db, user_id, request_id)
        # Delete the user row while build is finishing
        db.execute(delete(User).where(User.id == user_id))
        db.commit()

    monkeypatch.setattr(tasks, "build_export", build_and_delete_user)

    result = tasks.build_gdpr_export(user.id, req_id)
    assert result == "gone"
    assert not export_path(req_id).exists()
    assert not export_path(req_id).with_suffix(".zip.tmp").exists()


def test_old_entry_without_status_left_unchanged(
    tasks_db, db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)

    old_entry = {"request_id": "old_export_req", "requested_at": "2025-01-01T00:00:00"}
    active_req_id = "export_new_req_12345"
    active_entry = {
        "request_id": active_req_id,
        "requested_at": utc_now().isoformat(),
        "status": "building",
        "expires_at": None,
    }

    user = _create_user(
        db_session,
        "mixed_entries@example.com",
        export_requests=[old_entry, active_entry],
    )

    result = tasks.build_gdpr_export(user.id, active_req_id)
    assert result == "ready"

    db_session.expire_all()
    user_row = db_session.get(User, user.id)
    assert len(user_row.data_export_requests) == 2
    assert user_row.data_export_requests[0] == old_entry
    assert user_row.data_export_requests[1]["status"] == "ready"


def test_delete_expired_exports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)
    monkeypatch.setattr(settings, "GDPR_EXPORT_EXPIRY_HOURS", 24)

    now = utc_now()
    now_ts = now.timestamp()

    # Expired zip: 25 hours old
    expired_zip = export_dir / "expired.zip"
    expired_zip.write_bytes(b"PK01")
    os.utime(expired_zip, (now_ts - 25 * 3600, now_ts - 25 * 3600))

    # Fresh zip: 2 hours old
    fresh_zip = export_dir / "fresh.zip"
    fresh_zip.write_bytes(b"PK02")
    os.utime(fresh_zip, (now_ts - 2 * 3600, now_ts - 2 * 3600))

    # Old tmp: 2 hours old (older than 1 hour)
    old_tmp = export_dir / "stale.zip.tmp"
    old_tmp.write_bytes(b"tmp01")
    os.utime(old_tmp, (now_ts - 2 * 3600, now_ts - 2 * 3600))

    # Fresh tmp: 10 minutes old
    fresh_tmp = export_dir / "fresh.zip.tmp"
    fresh_tmp.write_bytes(b"tmp02")
    os.utime(fresh_tmp, (now_ts - 600, now_ts - 600))

    deleted = delete_expired_exports(now)
    assert deleted == 2

    assert not expired_zip.exists()
    assert not old_tmp.exists()
    assert fresh_zip.exists()
    assert fresh_tmp.exists()

    # Missing directory returns 0
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", tmp_path / "nonexistent")
    assert delete_expired_exports(now) == 0


def test_cleanup_expired_gdpr_exports_task(
    tasks_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(settings, "GDPR_EXPORT_DIR", export_dir)

    now_ts = utc_now().timestamp()
    expired_zip = export_dir / "expired_task.zip"
    expired_zip.write_bytes(b"PK...")
    os.utime(expired_zip, (now_ts - 30 * 3600, now_ts - 30 * 3600))

    count = tasks.cleanup_expired_gdpr_exports()
    assert count == 1
    assert not expired_zip.exists()


def _building(request_id: str) -> list[dict]:
    return [{"request_id": request_id, "requested_at": utc_now().isoformat(),
             "status": "building", "expires_at": None}]


def test_every_row_of_a_list_is_in_its_json_array(tasks_db, db_session) -> None:
    request_id = "export_" + "1" * 32
    user = _create_user(db_session, "rows@example.com", _building(request_id))
    for street in ("1 First Street", "2 Second Street", "3 Third Street"):
        db_session.add(Address(user_id=user.id, street=street, city="Metropolis",
                               state="NY", postal_code="10001", country="US"))
    db_session.commit()

    assert tasks.build_gdpr_export(user.id, request_id) == "ready"

    with zipfile.ZipFile(export_path(request_id)) as zf:
        streets = [a["street"] for a in json.loads(zf.read("addresses.json"))]
    assert streets == ["1 First Street", "2 Second Street", "3 Third Street"]


def test_an_archive_that_cannot_be_put_in_place_leaves_no_tmp_file(tasks_db, db_session) -> None:
    request_id = "export_" + "2" * 32
    user = _create_user(db_session, "blocked@example.com", _building(request_id))
    # A directory where the archive should go: the final rename fails after
    # the whole archive was written.
    export_path(request_id).mkdir(parents=True)

    assert tasks.build_gdpr_export(user.id, request_id) == "failed"

    assert not export_path(request_id).with_suffix(".zip.tmp").exists()


def test_build_export_for_a_missing_user_raises_and_writes_nothing(tasks_db) -> None:
    from app.services.gdpr_export import build_export

    with tasks.task_session() as db, pytest.raises(ValueError, match="not found"):
        build_export(db, 999_999, "export_" + "3" * 32)

    assert not any(settings.GDPR_EXPORT_DIR.iterdir())


def test_a_status_update_for_an_entry_removed_meanwhile_deletes_the_archive(tasks_db, db_session) -> None:
    from app.services.gdpr_export import update_export_status

    request_id = "export_" + "4" * 32
    user = _create_user(db_session, "removed@example.com", [])
    export_path(request_id).parent.mkdir(parents=True, exist_ok=True)
    export_path(request_id).write_bytes(b"zip")

    with tasks.task_session() as db:
        assert update_export_status(db, user.id, request_id, status="ready") == "gone"

    assert not export_path(request_id).exists()
