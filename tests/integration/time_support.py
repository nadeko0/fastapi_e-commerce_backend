"""Shared by the tests that freeze the clock with a local zone far from UTC."""
import time

import freezegun.api
import freezegun.config


def local_zone(monkeypatch, hours):
    """Inside freeze_time(..., tz_offset=hours): make the local zone `hours`
    from UTC the way a machine in that zone has it. freezegun shifts naive
    datetime.now() by tz_offset but also, wrongly, an aware
    datetime.now(timezone.utc) (which PyJWT uses to check tokens), and its
    time.localtime() follows time.timezone, not tz_offset. Both are put
    right here: aware times stay UTC, local ones are shifted."""
    real_now = freezegun.api.FakeDatetime.now.__func__

    def _now(cls, tz=None):
        if tz is None:
            return real_now(cls)
        return real_now(cls, tz) - cls._tz_offset()

    monkeypatch.setattr(freezegun.api.FakeDatetime, "now", classmethod(_now))
    monkeypatch.setattr(time, "timezone", -hours * 3600)
    # freezegun answers the real time to any call made under a "threading"
    # frame, which is every request TestClient serves: frozen everywhere.
    monkeypatch.setattr(freezegun.config.settings, "default_ignore_list", [])
