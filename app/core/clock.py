"""The one place that knows what "now" is, and how timestamps reach the database.

Every timestamp in the application is an aware datetime in UTC.
datetime.utcnow() is deprecated since Python 3.12 because it returns a naive
datetime that carries no hint of being UTC (and .timestamp() on a naive
datetime reads it as local time); datetime.now(UTC) is the documented
replacement (https://docs.python.org/3/library/datetime.html).

UTCDateTime follows the TZDateTime recipe of the SQLAlchemy documentation
(https://docs.sqlalchemy.org/en/20/core/custom_types.html). PostgreSQL stores
TIMESTAMP WITH TIME ZONE and returns aware values; SQLite has no such type and
returns naive ones (SQLAlchemy documents that SQLite keeps dates as strings),
so the type attaches UTC on the way out. Whatever the database, application
code only ever sees aware UTC datetimes.
"""
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """An aware datetime in UTC. A naive value is taken to be UTC already."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# For a model that reads timestamps written by older code or by another
# system as strings (Redis carts: naive isoformat, always UTC): naive input is
# taken as UTC, so the model only ever holds aware UTC datetimes.
UTCDatetime = Annotated[datetime, AfterValidator(as_utc)]


class UTCDateTime(TypeDecorator):
    """TIMESTAMP WITH TIME ZONE that reads back as aware UTC on every database."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else as_utc(value)

    def process_result_value(self, value, dialect):
        return None if value is None else as_utc(value)
