"""Local wall times in real zones reach the application as one UTC instant.

A client may write a time in its own zone. Around a clock change a wall time
is ambiguous (the hour that happens twice when clocks go back) or does not
exist (the hour skipped when they go forward). PEP 495 gives both a meaning
through `fold` (https://peps.python.org/pep-0495/): fold=0 is the offset in
force before the change, fold=1 the one after. The application never sees
the wall time: the query parameters are AwareDatetime, so the offset is
written in the value, and as_utc turns it into an instant. These tests run
real zones (Europe/Bratislava and America/New_York, which change clocks, and
Asia/Kolkata, which does not) through that path and compare with what Python
itself says the instant is.
"""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import AwareDatetime, TypeAdapter, ValidationError

from app.core.clock import as_utc


def _wall(year, month, day):
    """A naive wall-clock date: the bounds of st.datetimes, and the base of a
    local time, are naive by contract."""
    return datetime(year, month, day, tzinfo=UTC).replace(tzinfo=None)


ZONES = [ZoneInfo(name) for name in ("Europe/Bratislava", "America/New_York", "Asia/Kolkata")]
CHANGE_DAYS = {  # zone -> (first, last) day of the 2026 clock changes
    "Europe/Bratislava": [_wall(2026, 3, 29), _wall(2026, 10, 25)],
    "America/New_York": [_wall(2026, 3, 8), _wall(2026, 11, 1)],
    "Asia/Kolkata": [_wall(2026, 3, 29), _wall(2026, 10, 25)],
}
HOUR = timedelta(hours=1)
AWARE = TypeAdapter(AwareDatetime)

anywhere = st.datetimes(
    min_value=_wall(2026, 1, 1), max_value=_wall(2026, 12, 31),
    timezones=st.sampled_from(ZONES),
)


@st.composite
def around_a_change(draw):
    """A wall time within the five hours around a change, either fold, in the
    zone that changes that day (and in Kolkata, which has none)."""
    zone = draw(st.sampled_from(ZONES))
    day = draw(st.sampled_from(CHANGE_DAYS[zone.key]))
    minutes = draw(st.integers(0, 5 * 60 - 1))
    return (day + timedelta(minutes=minutes)).replace(tzinfo=zone, fold=draw(st.sampled_from([0, 1])))


def python_says(value):
    """The instant of a local time as PEP 495 defines it, through POSIX time.
    Not `==`: PEP 495 makes an aware time whose offset depends on fold (an
    ambiguous or missing one) unequal to every time in another zone."""
    return datetime.fromtimestamp(value.timestamp(), UTC)


def test_the_tz_database_is_present():
    # On Windows zoneinfo needs the tzdata package (a dependency of kombu,
    # psycopg and tzlocal there); on Linux the system database.
    for zone in ZONES:
        assert zone.utcoffset(_wall(2026, 7, 1)) is not None


@given(anywhere)
@example(datetime(2026, 10, 25, 2, 30, tzinfo=ZONES[0], fold=0))
@example(datetime(2026, 10, 25, 2, 30, tzinfo=ZONES[0], fold=1))
@example(datetime(2026, 3, 29, 2, 30, tzinfo=ZONES[0], fold=0))
@example(datetime(2026, 3, 29, 2, 30, tzinfo=ZONES[0], fold=1))
def test_a_local_time_is_the_instant_python_says(value):
    instant = as_utc(value)

    assert instant.utcoffset() == timedelta(0)
    assert instant == python_says(value)
    assert instant == value.astimezone(UTC)


@given(st.one_of(anywhere, around_a_change()))
def test_the_query_parameter_path_keeps_the_instant(value):
    """What a client sends is the ISO string with its offset; it comes out of
    AwareDatetime and as_utc as the same instant, and the JSON the API writes
    for it (UTC, "Z") reads back as that instant."""
    sent = value.isoformat()

    received = as_utc(AWARE.validate_python(sent))

    assert received == python_says(value)
    written = AWARE.dump_json(received).decode()
    assert written.endswith('Z"')
    assert AWARE.validate_json(written) == received


@given(around_a_change())
def test_a_wall_time_without_an_offset_is_refused(value):
    with pytest.raises(ValidationError) as refused:
        AWARE.validate_python(value.replace(tzinfo=None).isoformat())

    assert refused.value.errors()[0]["type"] == "timezone_aware"


@given(around_a_change())
def test_the_two_folds_of_a_changing_hour_are_exactly_an_hour_apart(value):
    first, second = value.replace(fold=0), value.replace(fold=1)
    if first.utcoffset() == second.utcoffset():
        return  # an ordinary time, or a zone without a change

    # Clocks back (ambiguous hour): the second pass is an hour after the
    # first. Clocks forward (missing hour): PEP 495 reads the nonexistent
    # time with the offset before the change for fold=0 and the one after for
    # fold=1, so the two readings are an hour apart the other way.
    assert abs(as_utc(second) - as_utc(first)) == HOUR
    assert as_utc(first).fold == as_utc(second).fold == 0


def test_the_two_02_30s_of_the_night_are_two_instants():
    local = ZoneInfo("Europe/Bratislava")
    first = datetime(2026, 10, 25, 2, 30, tzinfo=local, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=local, fold=1)

    assert as_utc(first) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert as_utc(second) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert first.isoformat().endswith("+02:00") and second.isoformat().endswith("+01:00")


def test_the_missing_02_30_of_march_has_a_defined_instant():
    local = ZoneInfo("Europe/Bratislava")
    early = datetime(2026, 3, 29, 2, 30, tzinfo=local, fold=0)
    late = datetime(2026, 3, 29, 2, 30, tzinfo=local, fold=1)

    assert as_utc(early) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # +01:00, before
    assert as_utc(late) == datetime(2026, 3, 29, 0, 30, tzinfo=UTC)   # +02:00, after
