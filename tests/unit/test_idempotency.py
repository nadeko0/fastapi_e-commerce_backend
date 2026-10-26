from app.core.config import settings
from app.core.idempotency import order_key_header_description


def test_order_key_header_description_includes_ttl():
    desc = order_key_header_description()
    assert str(settings.IDEMPOTENCY_KEY_TTL_HOURS) in desc
    assert "None" not in desc
