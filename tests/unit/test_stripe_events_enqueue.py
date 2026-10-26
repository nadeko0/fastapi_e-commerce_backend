import logging
from unittest.mock import patch

from app.services.stripe_events import enqueue_event


def test_enqueue_event_success():
    with patch("app.tasks.process_stripe_event.apply_async") as mock_apply:
        assert enqueue_event("evt_test_123") is True
        mock_apply.assert_called_once_with(args=["evt_test_123"], retry=False)


def test_enqueue_event_broker_outage_logs_one_line_without_traceback(caplog):
    with (
        patch(
            "app.tasks.process_stripe_event.apply_async",
            side_effect=ConnectionError("Error 111 connecting to redis:6379. Connection refused."),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = enqueue_event("evt_test_outage")

    assert result is False
    records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "evt_test_outage" in r.getMessage()
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec.exc_info is None
    assert "evt_test_outage" in rec.getMessage()
    assert "ConnectionError" in rec.getMessage()
    assert "Error 111 connecting to redis:6379. Connection refused." in rec.getMessage()
    assert "the sweeper re-queues it" in rec.getMessage()
    assert "\n" not in rec.getMessage()
