"""
EmailProvider abstraction. Deliberately minimal - a single `send` primitive -
mirroring how app/services/payment/base.py keeps PaymentProvider to just the
operations callers actually use rather than a speculative full mail-API
surface (no attachments, templates, bulk-send, etc - nothing in this codebase
needs them).
"""
from abc import ABC, abstractmethod


class EmailProvider(ABC):
    @abstractmethod
    def send(self, to_email: str, subject: str, html_content: str) -> bool:
        """Send a single HTML email.

        Must never raise: callers (background tasks, Celery jobs) are not
        written to handle an email failure as an exception - a failed send
        is reported by returning False (and should be logged by the
        implementation), matching the resilience contract the original
        EmailService._send_email had.
        """
        raise NotImplementedError
