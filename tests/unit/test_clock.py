"""app.core.clock: utc_now, and UTCDateTime, the column type that makes every
timestamp read from the database an aware UTC datetime on SQLite (which keeps
no time zone) and on PostgreSQL alike.

The type follows the TZDateTime recipe of the SQLAlchemy documentation:
https://docs.sqlalchemy.org/en/20/core/custom_types.html
"""
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel
from sqlalchemy import Column, Integer, create_engine, select
from sqlalchemy.orm import Session, declarative_base

from app.core.clock import UTCDateTime, UTCDatetime, as_utc, utc_now

KATHMANDU = timezone(timedelta(hours=5, minutes=45))

Base = declarative_base()


class Stamped(Base):
    __tablename__ = "stamped"
    id = Column(Integer, primary_key=True)
    at = Column(UTCDateTime)
    made = Column(UTCDateTime, default=utc_now)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_utc_now_is_aware_and_in_utc():
    now = utc_now()
    assert now.tzinfo is UTC
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


def test_as_utc_takes_a_naive_value_as_utc():
    # DTZ001: the naive value is the input under test.
    assert as_utc(datetime(2026, 10, 1, 12, 0)) == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # noqa: DTZ001


def test_as_utc_converts_an_aware_value():
    converted = as_utc(datetime(2026, 10, 1, 17, 45, tzinfo=KATHMANDU))
    assert converted == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert converted.utcoffset() == timedelta(0)


def test_the_column_is_timestamp_with_time_zone():
    assert UTCDateTime().impl.timezone is True
    assert UTCDateTime.cache_ok is True


def test_a_naive_value_goes_in_as_utc_and_comes_out_aware(session):
    # DTZ001: the naive value is the input under test.
    session.add(Stamped(id=1, at=datetime(2026, 10, 1, 12, 0)))  # noqa: DTZ001
    session.commit()
    session.expire_all()

    assert session.get(Stamped, 1).at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert session.get(Stamped, 1).at.tzinfo is not None


def test_an_aware_value_in_another_zone_comes_out_as_the_same_instant_in_utc(session):
    session.add(Stamped(id=1, at=datetime(2026, 10, 1, 17, 45, tzinfo=KATHMANDU)))
    session.commit()
    session.expire_all()

    value = session.get(Stamped, 1).at
    assert value == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert value.utcoffset() == timedelta(0)


def test_null_stays_null_and_the_default_is_aware(session):
    session.add(Stamped(id=1))
    session.commit()
    session.expire_all()

    row = session.get(Stamped, 1)
    assert row.at is None
    assert row.made.tzinfo is not None


def test_a_comparison_with_a_value_in_another_zone_compares_instants(session):
    session.add(Stamped(id=1, at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC)))
    session.commit()

    same_instant = datetime(2026, 10, 1, 17, 45, tzinfo=KATHMANDU)
    found = session.scalars(select(Stamped).where(Stamped.at >= same_instant)).all()
    later = session.scalars(select(Stamped).where(Stamped.at > same_instant)).all()
    assert len(found) == 1 and later == []


def test_utcdatetime_annotation_attaches_utc_to_a_naive_string():
    class Model(BaseModel):
        at: UTCDatetime

    assert Model(at="2026-10-01T12:00:00").at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert Model(at="2026-10-01T17:45:00+05:45").at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_the_deprecated_utcnow_fails_the_suite():
    # pyproject.toml turns its DeprecationWarning into an error, so a new use
    # of datetime.utcnow() cannot slip in unnoticed.
    with pytest.raises(DeprecationWarning):
        datetime.utcnow()  # noqa: DTZ003 - the deprecated call is what is under test
