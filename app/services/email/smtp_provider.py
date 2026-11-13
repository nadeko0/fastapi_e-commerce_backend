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
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser

from app.core.config import settings
from app.services.email.base import EmailProvider, mask_email

logger = logging.getLogger(__name__)


class _PlainText(HTMLParser):
    """The text/plain alternative of an HTML mail: block elements end lines,
    list items get a dash, table cells are separated by " | ", links keep
    their address, <pre> keeps its layout, and head/style/script are
    dropped. Not a renderer: the templates (messages.py) are paragraphs,
    headings, simple tables and lists. Character references are decoded."""

    _SKIPPED = frozenset({"head", "title", "style", "script"})
    # Set off by a blank line, like paragraphs.
    _PARAGRAPHS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre", "hr"})
    # Start a new line.
    _LINES = frozenset({"tr", "li", "br"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skipping = 0
        self._in_pre = 0
        self._href: str | None = None

    def _at_line_start(self) -> bool:
        return not self._out or self._out[-1].endswith("\n")

    def _break(self, newlines: int) -> None:
        """End the current line, and leave `newlines` - 1 blank lines after
        it, unless the output already ends that way."""
        text = "".join(self._out).rstrip(" ")
        if not text:
            return
        missing = newlines - (len(text) - len(text.rstrip("\n")))
        self._out = [text + "\n" * max(0, missing)]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
            return
        if tag in self._PARAGRAPHS:
            self._break(2)
        elif tag in self._LINES:
            self._break(1)
        if tag == "li":
            self._out.append("- ")
        elif tag in ("td", "th") and not self._at_line_start():
            self._out[-1] = self._out[-1].rstrip(" ")
            self._out.append(" | ")
        elif tag == "pre":
            self._in_pre += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skipping = max(0, self._skipping - 1)
            return
        if tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
        elif tag == "a" and self._href:
            self._out.append(f" ({self._href})")
            self._href = None
        if tag in self._PARAGRAPHS:
            self._break(2)

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        if not self._in_pre:
            data = re.sub(r"\s+", " ", data)
            if self._at_line_start():
                data = data.lstrip(" ")
        if data:
            self._out.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._out)).strip() + "\n"


def html_to_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    parser.close()
    return parser.text()


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
        # RFC 5322 3.6: Date is required and Message-ID should be present;
        # smtplib adds neither, and providers score mail without them as spam.
        message['Date'] = formatdate(localtime=False)
        # The sender's domain, not make_msgid's default (this host's name,
        # looked up by DNS and meaningless outside the container).
        message['Message-ID'] = make_msgid(domain=self.from_email.rpartition('@')[2])
        # RFC 2046 5.1.4: the last alternative is the preferred one, so the
        # plain text goes first, for clients that do not render HTML.
        message.attach(MIMEText(html_to_text(html_content), 'plain'))
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
