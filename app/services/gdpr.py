from datetime import timedelta

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.user import User
from app.schemas.legal import ConsentUpdate
from app.schemas.order import OrderStatus
from app.schemas.user import GDPRExport
from app.services.orders import (
    cancel_order_and_restock,
    close_attempts_at_provider,
    is_unpaid_and_new,
    open_payment_attempts,
    products_with_cached_stock,
)
from app.services.payment import get_payment_provider
from app.services.redis_async import AsyncRedisService

CONSENT_USER_AGENT_MAX = 512


def consent_user_agent(request: Request) -> str | None:
    """The User-Agent to store in a consent history entry. Cut short: the
    header can be ~16 KiB, the entries are kept for good and the row is
    loaded on every authenticated request."""
    value = request.headers.get("user-agent")
    return None if value is None else value[:CONSENT_USER_AGENT_MAX]


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

        db = self.db
        assert db is not None, "Database session required for consent update"
        try:
            await db.commit()
            await db.refresh(user)
            return {
                "marketing": user.marketing_consent,
                "privacy_policy": user.privacy_policy_accepted,

            }
        except Exception as e:
            await db.rollback()
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

        db = self.db
        assert db is not None, "Database session required for collecting export"
        user = await db.get(User, user_id)
        addresses = (await db.scalars(
            select(Address).where(Address.user_id == user_id).order_by(Address.id)
        )).all()
        orders = (await db.scalars(
            select(Order).options(selectinload(Order.items))
            .where(Order.user_id == user_id).order_by(Order.id)
        )).all()
        payments = (await db.scalars(
            select(Payment).join(Order, Order.id == Payment.order_id)
            .where(Order.user_id == user_id).order_by(Payment.id)
        )).all()
        refunds = (await db.scalars(
            select(PaymentRefund).join(Payment, Payment.id == PaymentRefund.payment_id)
            .join(Order, Order.id == Payment.order_id)
            .where(Order.user_id == user_id).order_by(PaymentRefund.id)
        )).all()
        await db.commit()  # no transaction stays open across the SMTP sends
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

    # The mails of both requests are queued on the route's BackgroundTasks,
    # as the order routes queue theirs: they go out after the response, in a
    # worker thread (Starlette runs a sync task in the threadpool). The
    # request's outcome is its commit. Sent inside the handler, as they
    # were, the client waited for SMTP (about 0.8 s a mail against a real
    # server), and a mail that raised answered 500 for a request already
    # recorded; the providers are written never to raise, but the answer no
    # longer depends on it. A task that raises is logged by the server
    # after the response and the tasks queued after it are not run.

    async def process_data_export(self, user: User, background_tasks: BackgroundTasks) -> str:
        """
        Process a GDPR data export request
        Returns a request ID that can be used to track the export status
        """
        # Generate unique request ID
        request_id = f"export_{user.id}_{utc_now().timestamp()}"

        try:
            # `user` may be the request's detached snapshot: the row is written
            # through this session.
            db = self.db
            assert db is not None, "Database session required for data export"
            row = await db.get(User, user.id)
            assert row is not None, "User must exist in database"
            requests = list(row.data_export_requests or [])
            requests.append({"request_id": request_id, "requested_at": utc_now().isoformat()})
            row.data_export_requests = requests
            await db.commit()

            # The export goes to the account's own address, in the mail. A
            # large account would rather get a signed, expiring download link.
            export_data = GDPRExport(
                request_id=request_id,
                request_date=utc_now(),
                expires_at=utc_now() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS),
                status="completed",
                data=await self.collect_export(user.id),
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail="Failed to process data export request"
            ) from e

        from app.services.email import send_gdpr_export_email, send_gdpr_request_received
        background_tasks.add_task(send_gdpr_request_received, user.email, "export", request_id)
        background_tasks.add_task(send_gdpr_export_email, user.email, export_data)
        return request_id

    async def process_data_deletion(
        self, user: User, cache: AsyncRedisService, background_tasks: BackgroundTasks
    ) -> str:
        """
        Process a GDPR data deletion request
        Returns a request ID that can be used to track the deletion status

        The account's new, unpaid orders are cancelled and restocked, and
        their open payment attempts closed at the provider: the deactivated
        owner could otherwise still pay one and get an order they cannot
        see (403 on every route). Paid orders are left as they are.
        """
        request_id = f"deletion_{user.id}_{utc_now().timestamp()}"

        db = self.db
        assert db is not None, "Database session required for data deletion"
        if user.role == "admin" and not await db.scalar(
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
            # Locked in id order like the cancel routes' read, so the expiry
            # sweep or an admin cancel cannot restock the same order again.
            orders = (await db.scalars(
                select(Order).options(selectinload(Order.items))
                .where(Order.user_id == user.id, Order.status == OrderStatus.NEW)
                .order_by(Order.id).with_for_update(of=Order)
            )).all()
            unpaid = [order for order in orders if is_unpaid_and_new(order)]
            attempts: list[tuple[str | None, str]] = []
            for order in unpaid:
                await db.run_sync(cancel_order_and_restock, order)
                attempts += await db.run_sync(open_payment_attempts, order.id)
            restocked = products_with_cached_stock(
                [(item.product_id, item.variant_id) for order in unpaid for item in order.items]
            )

            user.is_active = False
            user.data_deletion_requested = True
            user.data_deletion_date = utc_now()

            await db.commit()

            # After the commit, as the cancel routes do (_drop_product_details,
            # close_attempts_at_provider). Inline rather than as a background
            # task like the mails below: the orders are unpayable by the time
            # the erasure is answered.
            for product_id in restocked:
                await cache.invalidate_product_cache(product_id)
            if attempts:
                await run_in_threadpool(
                    close_attempts_at_provider, get_payment_provider(), attempts
                )

        except Exception as e:
            await db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to process deletion request"
            ) from e

        # After the commit (2D-03), queued (see the note above process_data_export).
        from app.services.email import (
            send_gdpr_deletion_confirmation,
            send_gdpr_request_received,
        )
        background_tasks.add_task(send_gdpr_request_received, user.email, "deletion", request_id)
        background_tasks.add_task(send_gdpr_deletion_confirmation, user.email, request_id)
        return request_id

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
