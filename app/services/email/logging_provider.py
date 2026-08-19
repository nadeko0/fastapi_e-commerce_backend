"""
LoggingEmailProvider - a no-op EmailProvider that only logs. Used for local
development (no SMTP server to hand) and as a safe default the test suite can
rely on so tests never risk touching a real network - mirrors how
StripePaymentProvider (app/services/payment/stripe_provider.py) is a
deterministic, dependency-free stand-in for the real thing.
"""
import logging

from app.services.email.base import EmailProvider

logger = logging.getLogger(__name__)


class LoggingEmailProvider(EmailProvider):
    def send(self, to_email: str, subject: str, html_content: str) -> bool:
        logger.info(f"Email suppressed (LoggingEmailProvider): to={to_email} subject={subject!r}")
        return True
