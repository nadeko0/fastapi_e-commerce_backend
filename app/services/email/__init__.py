from app.core.config import settings
from app.services.email.base import EmailProvider
from app.services.email.logging_provider import LoggingEmailProvider
from app.services.email.smtp_provider import SmtpEmailProvider


def get_email_provider() -> EmailProvider:
    """Factory - swap this to switch email providers.

    EMAIL_PROVIDER=logging (the default outside production) suppresses real
    sends and only logs, so local dev and the test suite never need a real
    SMTP server. Set EMAIL_PROVIDER=smtp to actually send mail.
    """
    if settings.EMAIL_PROVIDER == "smtp":
        return SmtpEmailProvider()
    return LoggingEmailProvider()


# Imported after get_email_provider is defined: messages.py calls back into
# this module (`from app.services.email import get_email_provider`) at
# import time, so the name must already exist on this module object.
from app.services.email.messages import (  # noqa: E402
    send_email_verification,
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
    "SmtpEmailProvider",
    "LoggingEmailProvider",
    "get_email_provider",
    "send_order_confirmation_email",
    "send_order_status_update_email",
    "send_order_cancellation_email",
    "send_low_stock_alert_email",
    "send_welcome_email",
    "send_email_verification",
    "send_password_reset_email",
    "send_gdpr_export_email",
    "send_gdpr_deletion_confirmation",
    "send_gdpr_request_received",
]
