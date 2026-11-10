from app.core.config import settings
from app.services.email.base import EmailProvider
from app.services.email.logging_provider import LoggingEmailProvider
from app.services.email.smtp_provider import SmtpEmailProvider


def get_email_provider() -> EmailProvider:
    """Factory - swap this to switch email providers.

    EMAIL_PROVIDER is "smtp" or "logging" (validated by Settings) and defaults
    to smtp in every environment. Set EMAIL_PROVIDER=logging (as the test
    suite does) to suppress real sends and only log, so local dev never needs
    a real SMTP server. Production refuses "logging".
    """
    if settings.EMAIL_PROVIDER == "smtp":
        return SmtpEmailProvider()
    return LoggingEmailProvider()


# Imported after get_email_provider is defined: messages.py calls back into
# this module (`from app.services.email import get_email_provider`) at
# import time, so the name must already exist on this module object.
from app.services.email.messages import (  # noqa: E402
    send_account_exists_email,
    send_email_verification,
    send_gdpr_deletion_completed,
    send_gdpr_deletion_confirmation,
    send_gdpr_export_email,
    send_gdpr_request_received,
    send_low_stock_alert_email,
    send_order_cancellation_email,
    send_order_confirmation_email,
    send_order_status_update_email,
    send_password_reset_email,
    send_welcome_email,
)

__all__ = [
    "EmailProvider",
    "LoggingEmailProvider",
    "SmtpEmailProvider",
    "get_email_provider",
    "send_account_exists_email",
    "send_email_verification",
    "send_gdpr_deletion_completed",
    "send_gdpr_deletion_confirmation",
    "send_gdpr_export_email",
    "send_gdpr_request_received",
    "send_low_stock_alert_email",
    "send_order_cancellation_email",
    "send_order_confirmation_email",
    "send_order_status_update_email",
    "send_password_reset_email",
    "send_welcome_email",
]
