"""
SmtpEmailProvider - today's real implementation, sending mail over SMTP via
smtplib. Behavior is unchanged from the original EmailService: any failure
(connection refused, auth failure, etc) is caught, logged, and reported back
as `False` - it never raises - since this runs from background tasks and
Celery jobs that aren't written to handle an email failure as an exception.

The only functional change from the original EmailService is that failures
are logged through the app's logger instead of printed to stdout, so they
actually show up in the JSON log pipeline (see app/core/logging_config.py)
rather than being lost.
"""
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.services.email.base import EmailProvider, mask_email

logger = logging.getLogger(__name__)


class SmtpEmailProvider(EmailProvider):
    def __init__(self):
        self.smtp_host = settings.SMTP_HOST
        self.smtp_port = settings.SMTP_PORT
        self.smtp_user = settings.SMTP_USER
        self.smtp_password = settings.SMTP_PASSWORD.get_secret_value()
        self.smtp_timeout = settings.SMTP_TIMEOUT_SECONDS
        self.tls_context = ssl.create_default_context()
        if not settings.SMTP_TLS_VERIFY:
            self.tls_context.check_hostname = False
            self.tls_context.verify_mode = ssl.CERT_NONE
        self.from_email = settings.EMAILS_FROM_EMAIL
        self.from_name = settings.EMAILS_FROM_NAME

    def _create_message(self, to_email: str, subject: str, html_content: str) -> MIMEMultipart:
        message = MIMEMultipart('alternative')
        message['Subject'] = subject
        message['From'] = f"{self.from_name} <{self.from_email}>"
        message['To'] = to_email
        message.attach(MIMEText(html_content, 'html'))
        return message

    def send(self, to_email: str, subject: str, html_content: str) -> bool:
        try:
            message = self._create_message(to_email, subject, html_content)
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout) as server:
                server.starttls(context=self.tls_context)
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(message)
            return True
        except Exception as e:  # noqa: BLE001 - any failure means "not sent"; callers get False, never an exception
            # Not logger.exception: smtplib's errors carry the recipient
            # (SMTPRecipientsRefused is keyed by address), and a traceback
            # would put it in the logs in full. The type and the SMTP code,
            # when there is one, are what diagnosing a failed send needs.
            code = getattr(e, "smtp_code", None)
            logger.error(
                "Failed to send email to %s (subject=%r): %s%s",
                mask_email(to_email),
                subject,
                type(e).__name__,
                f" {code}" if code else "",
            )
            return False
