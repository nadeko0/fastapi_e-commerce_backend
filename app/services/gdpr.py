from datetime import timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_now
from app.core.config import settings
from app.models.user import User
from app.schemas.legal import ConsentUpdate
from app.schemas.user import GDPRExport


def _row(obj, fields: tuple[str, ...]) -> dict:
    """The named columns of `obj`, as JSON-ready values."""
    out = {}
    for name in fields:
        value = getattr(obj, name)
        out[name] = value.isoformat() if hasattr(value, "isoformat") else (
            str(value) if value is not None and not isinstance(value, (bool, int, str, list, dict))
            else value
        )
    return out


_USER_FIELDS = (
    "id", "email", "full_name", "phone", "created_at", "last_login", "is_email_verified",
    "marketing_consent", "marketing_consent_date", "privacy_policy_accepted",
    "privacy_policy_accepted_date", "consent_history",
)
_ADDRESS_FIELDS = (
    "id", "street", "city", "state", "postal_code", "country", "address_type",
    "delivery_phone", "delivery_instructions", "is_active", "created_at",
)
_ORDER_FIELDS = (
    "id", "status", "payment_status", "total_amount", "currency", "shipping_address_id",
    "created_at",
)
_ITEM_FIELDS = ("product_id", "variant_id", "quantity", "price_at_time")
_PAYMENT_FIELDS = ("id", "order_id", "amount", "currency", "status", "created_at")
_REFUND_FIELDS = ("payment_id", "amount", "currency", "status", "created_at")


class GDPRService:
    # `db` is only needed by the two methods that write (update_user_consent,
    # process_data_deletion); the pure ones (get_consent_status,
    # validate_retention_period) never touch it, so a route that only calls
    # those builds the service without a session.
    def __init__(self, db: AsyncSession | None = None):
        self.db = db

    async def update_user_consent(
        self,
        user: User,
        consent_update: ConsentUpdate,
        ip_address: str | None = None,
        user_agent: str | None = None
    ) -> dict[str, bool]:
        """
        Update user's consent preferences and maintain consent history
        """
        current_time = utc_now()

        # Create consent history entry
        consent_entries = []

        # Entry shape matches the ones register_user writes so that
        # consent_history is consistent (and demonstrable per Art. 7(1)).
        # Handle marketing consent
        if user.marketing_consent != consent_update.marketing_consent:
            consent_entries.append({
                "type": "marketing",
                "granted": consent_update.marketing_consent,
                "timestamp": current_time.isoformat(),
                "ip_address": ip_address,
                "user_agent": user_agent
            })
            user.marketing_consent = consent_update.marketing_consent
            user.marketing_consent_date = current_time

        # Handle privacy policy acceptance
        if user.privacy_policy_accepted != consent_update.privacy_policy_accepted:
            consent_entries.append({
                "type": "privacy_policy",
                "granted": consent_update.privacy_policy_accepted,
                "timestamp": current_time.isoformat(),
                "ip_address": ip_address,
                "user_agent": user_agent
            })
            user.privacy_policy_accepted = consent_update.privacy_policy_accepted
            user.privacy_policy_accepted_date = current_time

        # Update consent history
        user.consent_history = (user.consent_history or []) + consent_entries

        try:
            await self.db.commit()
            await self.db.refresh(user)
            return {
                "marketing": user.marketing_consent,
                "privacy_policy": user.privacy_policy_accepted,

            }
        except Exception as e:
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to update consent preferences"
            ) from e

    async def collect_export(self, user_id: int) -> dict:
        """Art. 15/20: the user's account, addresses, orders with their
        lines, payments and refunds. Bounded queries, eager-loaded lines."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.models.address import Address
        from app.models.order import Order
        from app.models.payment import Payment
        from app.models.payment_ledger import PaymentRefund

        user = await self.db.get(User, user_id)
        addresses = (await self.db.scalars(
            select(Address).where(Address.user_id == user_id).order_by(Address.id)
        )).all()
        orders = (await self.db.scalars(
            select(Order).options(selectinload(Order.items))
            .where(Order.user_id == user_id).order_by(Order.id)
        )).all()
        payments = (await self.db.scalars(
            select(Payment).join(Order, Order.id == Payment.order_id)
            .where(Order.user_id == user_id).order_by(Payment.id)
        )).all()
        refunds = (await self.db.scalars(
            select(PaymentRefund).join(Payment, Payment.id == PaymentRefund.payment_id)
            .join(Order, Order.id == Payment.order_id)
            .where(Order.user_id == user_id).order_by(PaymentRefund.id)
        )).all()
        await self.db.commit()  # no transaction stays open across the SMTP sends
        return {
            "user": _row(user, _USER_FIELDS),
            "addresses": [_row(a, _ADDRESS_FIELDS) for a in addresses],
            "orders": [
                {**_row(o, _ORDER_FIELDS), "items": [_row(i, _ITEM_FIELDS) for i in o.items]}
                for o in orders
            ],
            "payments": [_row(p, _PAYMENT_FIELDS) for p in payments],
            "refunds": [_row(r, _REFUND_FIELDS) for r in refunds],
        }

    async def process_data_export(self, user: User) -> str:
        """
        Process a GDPR data export request
        Returns a request ID that can be used to track the export status
        """
        # Generate unique request ID
        request_id = f"export_{user.id}_{utc_now().timestamp()}"

        try:
            # Send initial confirmation email. In a worker thread, like every
            # send below: these functions are synchronous SMTP (connect,
            # STARTTLS, login, send - about 0.8 s against a real server), and
            # this method runs on the event loop. Called inline, as they were,
            # each one stopped the whole worker for that long; this was the
            # only route still sending mail from inside a handler (found by
            # tracing an end-to-end run, docs/reports/load-testing-3x.md).
            from app.services.email import send_gdpr_request_received
            await run_in_threadpool(send_gdpr_request_received, user.email, "export", request_id)

            # `user` may be the request's detached snapshot: the row is written
            # through this session.
            row = await self.db.get(User, user.id)
            requests = list(row.data_export_requests or [])
            requests.append({"request_id": request_id, "requested_at": utc_now().isoformat()})
            row.data_export_requests = requests
            await self.db.commit()

            # The export goes to the account's own address, inline. A large
            # account would rather get a signed, expiring download link.
            from app.services.email import send_gdpr_export_email
            export_data = GDPRExport(
                request_id=request_id,
                request_date=utc_now(),
                expires_at=utc_now() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS),
                status="completed",
                data=await self.collect_export(user.id),
            )
            await run_in_threadpool(send_gdpr_export_email, user.email, export_data)

            return request_id

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail="Failed to process data export request"
            ) from e

    async def process_data_deletion(self, user: User) -> str:
        """
        Process a GDPR data deletion request
        Returns a request ID that can be used to track the deletion status
        """
        request_id = f"deletion_{user.id}_{utc_now().timestamp()}"

        if user.role == "admin" and not await self.db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.is_active.is_(True), User.id != user.id
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="The only active administrator cannot be erased",
            )

        try:
            # Mark user for deletion and deactivate immediately, as
            # an erasure request always does. The scheduled cleanup_inactive_accounts
            # Celery task (app/tasks.py) anonymises the account once
            # DATA_DELETION_GRACE_PERIOD_DAYS has passed. Committed before any
            # mail is sent: SMTP inside the open transaction (2D-03) could
            # outlast idle_in_transaction_session_timeout.
            user.is_active = False
            user.data_deletion_requested = True
            user.data_deletion_date = utc_now()

            await self.db.commit()

            # Send emails after commit (2D-03)
            from app.services.email import (
                send_gdpr_deletion_confirmation,
                send_gdpr_request_received,
            )
            await run_in_threadpool(
                send_gdpr_request_received, user.email, "deletion", request_id
            )
            await run_in_threadpool(send_gdpr_deletion_confirmation, user.email, request_id)

            return request_id

        except Exception as e:
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to process deletion request"
            ) from e

    def get_consent_status(self, user: User) -> dict:
        """
        Get current consent status for a user
        """
        # GET /legal/consent-status (app/api/v1/legal.py) declares
        # response_model=UserConsent, which requires marketing_consent_date
        # and privacy_policy_accepted_date - omitting them here always
        # raised a 500 ResponseValidationError on every call.
        return {
            "marketing_consent": user.marketing_consent,
            "marketing_consent_date": user.marketing_consent_date,
            "privacy_policy_accepted": user.privacy_policy_accepted,
            "privacy_policy_accepted_date": user.privacy_policy_accepted_date,
            "consent_history": user.consent_history
        }

    def validate_retention_period(self, user: User) -> bool:
        """
        Validate if user data is within retention period
        """
        if not user.created_at:
            return True

        retention_days = user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
        return utc_now() - user.created_at <= timedelta(days=retention_days)
