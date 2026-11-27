import io
import json
import logging
import os
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import PaymentRefund
from app.models.user import User
from app.services.gdpr import (
    _ADDRESS_FIELDS,
    _ITEM_FIELDS,
    _ORDER_FIELDS,
    _PAYMENT_FIELDS,
    _REFUND_FIELDS,
    _USER_FIELDS,
    _row,
)

logger = logging.getLogger(__name__)


def export_path(request_id: str) -> Path:
    """Return the filesystem destination path for a GDPR data export archive."""
    return Path(settings.GDPR_EXPORT_DIR) / f"{request_id}.zip"


def delete_export_files(request_id: str) -> None:
    """Remove any archive and temporary archive files for a request ID."""
    final = export_path(request_id)
    final.unlink(missing_ok=True)
    final.with_suffix(".zip.tmp").unlink(missing_ok=True)


def has_export_request(user: User, request_id: str) -> bool:
    """Check if the user has an active export request matching request_id."""
    entries = user.data_export_requests or []
    return any(
        isinstance(e, dict) and e.get("request_id") == request_id and "status" in e
        for e in entries
    )


def update_export_status(
    db: Session,
    user_id: int,
    request_id: str,
    status: str,
    expires_at: datetime | None = None,
) -> str:
    """Update the status of an export request on the User row with row locking."""
    user = db.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        delete_export_files(request_id)
        return "gone"

    requests = list(user.data_export_requests or [])
    updated = False
    new_requests = []
    for item in requests:
        if isinstance(item, dict) and item.get("request_id") == request_id and "status" in item:
            new_item = dict(item)
            new_item["status"] = status
            if expires_at is not None:
                new_item["expires_at"] = expires_at.isoformat()
            new_requests.append(new_item)
            updated = True
        else:
            new_requests.append(item)

    if not updated:
        delete_export_files(request_id)
        return "gone"

    user.data_export_requests = new_requests
    db.commit()
    return status


def _write_json_array(
    zf: zipfile.ZipFile, arcname: str, elements: Iterable[dict[str, Any]]
) -> None:
    """Stream an iterable of JSON-serializable dicts as a JSON array into a zip entry."""
    with zf.open(arcname, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8") as writer:
        writer.write("[")
        first = True
        for element in elements:
            if not first:
                writer.write(",")
            else:
                first = False
            json.dump(element, writer)
        writer.write("]")


def build_export(db: Session, user_id: int, request_id: str) -> None:
    """Build the GDPR Article 15 export archive streaming rows directly to disk."""
    dest_path = export_path(request_id)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(".zip.tmp")

    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            with zf.open("account.json", "w") as raw, io.TextIOWrapper(raw, encoding="utf-8") as writer:
                json.dump(_row(user, _USER_FIELDS), writer)

            addr_stmt = (
                select(Address)
                .where(Address.user_id == user_id)
                .order_by(Address.id)
                .execution_options(yield_per=500)
            )
            _write_json_array(
                zf,
                "addresses.json",
                (_row(a, _ADDRESS_FIELDS) for a in db.scalars(addr_stmt)),
            )

            order_stmt = (
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.user_id == user_id)
                .order_by(Order.id)
                .execution_options(yield_per=500)
            )
            _write_json_array(
                zf,
                "orders.json",
                (
                    {
                        **_row(o, _ORDER_FIELDS),
                        "items": [_row(i, _ITEM_FIELDS) for i in o.items],
                    }
                    for o in db.scalars(order_stmt)
                ),
            )

            payment_stmt = (
                select(Payment)
                .join(Order, Order.id == Payment.order_id)
                .where(Order.user_id == user_id)
                .order_by(Payment.id)
                .execution_options(yield_per=500)
            )
            _write_json_array(
                zf,
                "payments.json",
                (_row(p, _PAYMENT_FIELDS) for p in db.scalars(payment_stmt)),
            )

            refund_stmt = (
                select(PaymentRefund)
                .join(Payment, Payment.id == PaymentRefund.payment_id)
                .join(Order, Order.id == Payment.order_id)
                .where(Order.user_id == user_id)
                .order_by(PaymentRefund.id)
                .execution_options(yield_per=500)
            )
            _write_json_array(
                zf,
                "refunds.json",
                (_row(r, _REFUND_FIELDS) for r in db.scalars(refund_stmt)),
            )

        os.replace(tmp_path, dest_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def delete_expired_exports(now: datetime) -> int:
    """Delete export archives older than GDPR_EXPORT_EXPIRY_HOURS and stale tmp files."""
    export_dir = Path(settings.GDPR_EXPORT_DIR)
    if not export_dir.exists() or not export_dir.is_dir():
        return 0

    now_ts = now.timestamp()
    zip_cutoff = now_ts - (settings.GDPR_EXPORT_EXPIRY_HOURS * 3600)
    tmp_cutoff = now_ts - 3600

    deleted = 0
    for path in export_dir.glob("*.zip"):
        if path.is_file() and path.stat().st_mtime < zip_cutoff:
            path.unlink(missing_ok=True)
            deleted += 1

    for path in export_dir.glob("*.zip.tmp"):
        if path.is_file() and path.stat().st_mtime < tmp_cutoff:
            path.unlink(missing_ok=True)
            deleted += 1

    logger.info("Deleted %d expired GDPR export archive(s)", deleted)
    return deleted
