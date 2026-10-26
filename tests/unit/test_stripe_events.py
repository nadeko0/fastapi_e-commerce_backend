import json

from app.services.stripe_events import _event_references, redact_event_payload


def test_event_references_extracts_object_id():
    payload = {"data": {"object": {"id": "cs_test_123"}}}
    assert "cs_test_123" in _event_references(payload)


def test_event_references_handles_non_dict_data():
    assert _event_references({"data": None}) == set()
    assert _event_references({"data": "string"}) == set()


def test_redact_event_payload_non_dict_data():
    result = json.loads(redact_event_payload('{"id": "evt_1", "data": null}'))
    assert result["data"]["object"] == {}
    assert result["redacted"] is True
