"""Security audit events: one log line per event on the `app.audit` logger.

Each line carries `event` (a stable name to search and alert on) and the
ids involved in `extra`, never an email, a password, a token or an IP
address; the request_id the JSON formatter adds ties the line to the
access log line of the same request. Failures and suspected attacks are
WARNING, normal security-relevant changes INFO.

Events: login.failed, refresh.replay, admin.denied, admin.action,
password.reset, logout, gdpr.export_requested, gdpr.deletion_requested.
"""

import logging
from typing import Any

logger = logging.getLogger("app.audit")


def audit(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, "audit %s", event, extra={"event": event, **fields})
