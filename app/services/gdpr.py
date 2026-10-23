from datetime import timedelta

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_now
from app.core.config import settings
from app.models.user import User
from app.schemas.legal import ConsentUpdate
from app.schemas.user import GDPRExport


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

        # Entry shape matches app/api/v1/users.py::update_consent so that
        # consent_history is consistent (and demonstrable per Art. 7(1))
        # regardless of which endpoint the client used.
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
        except Exception:
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to update consent preferences"
            )

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
            # tracing an end-to-end run, scripts/loadtest/FINDINGS.md).
            from app.services.email import send_gdpr_request_received
            await run_in_threadpool(send_gdpr_request_received, user.email, "export", request_id)

            # In production, the export payload would be assembled here and
            # stored in a secure location with a signed, expiring download
            # link (the synchronous GET /users/data/export endpoint already
            # returns the full export inline instead - see users.py).
            from app.services.email import send_gdpr_export_email
            export_data = GDPRExport(
                request_id=request_id,
                request_date=utc_now(),
                expires_at=utc_now() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS)
            )
            await run_in_threadpool(send_gdpr_export_email, user.email, export_data)

            return request_id

        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Failed to process data export request"
            )

    async def process_data_deletion(self, user: User) -> str:
        """
        Process a GDPR data deletion request
        Returns a request ID that can be used to track the deletion status
        """
        request_id = f"deletion_{user.id}_{utc_now().timestamp()}"

        try:
            # Send initial confirmation email
            from app.services.email import send_gdpr_request_received
            await run_in_threadpool(
                send_gdpr_request_received, user.email, "deletion", request_id
            )

            # Mark user for deletion and deactivate immediately, matching
            # POST /users/data/delete. The scheduled cleanup_inactive_accounts
            # Celery task (app/tasks.py) performs the actual hard delete once
            # DATA_DELETION_GRACE_PERIOD_DAYS has passed.
            user.is_active = False
            user.data_deletion_requested = True
            user.data_deletion_date = utc_now()

            await self.db.commit()

            # Send deletion confirmation email
            from app.services.email import send_gdpr_deletion_confirmation
            await run_in_threadpool(send_gdpr_deletion_confirmation, user.email, request_id)

            return request_id

        except Exception:
            await self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to process deletion request"
            )

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
        age_in_days = (utc_now() - user.created_at).days
        return age_in_days <= retention_days
