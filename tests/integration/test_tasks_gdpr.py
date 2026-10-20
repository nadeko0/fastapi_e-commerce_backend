from datetime import datetime, timedelta

from sqlalchemy import event

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.user import User
from app.tasks import purge_expired_deletion_requests


def _make_deletion_requested_user(db_session, email, days_since_request):
    user = User(
        email=email,
        hashed_password=get_password_hash("Str0ngPass1"),
        gdpr_consent=True,
        privacy_policy_accepted=True,
        is_active=False,
        data_deletion_requested=True,
        data_deletion_date=datetime.utcnow() - timedelta(days=days_since_request),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_purge_anonymizes_accounts_past_grace_period(db_session):
    # Regression test: cleanup_inactive_accounts used to gate the purge on
    # INACTIVE_ACCOUNT_DELETE_DAYS (730 days) even though the deletion
    # endpoints tell users their account "will be permanently deleted
    # within 24 hours". purge_expired_deletion_requests (extracted from
    # the Celery task) must use DATA_DELETION_GRACE_PERIOD_DAYS instead.
    expired = _make_deletion_requested_user(
        db_session, "expired@example.com",
        days_since_request=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1,
    )
    original_id = expired.id

    purged_count = purge_expired_deletion_requests(db_session)

    assert purged_count == 1
    # Regression test: the User row (and its cascade="all, delete-orphan"
    # orders/order items) used to be hard-deleted outright, which would
    # also wipe the account's order/invoice history. Most EU member states
    # require invoices retained ~10 years for tax purposes (GDPR Art.
    # 17(3)(b) exempts data still needed for a legal obligation), so the
    # row must survive with its personal identifiers scrubbed instead.
    scrubbed = db_session.query(User).filter(User.id == original_id).first()
    assert scrubbed is not None
    assert scrubbed.email == f"deleted-user-{original_id}@deleted.invalid"
    assert scrubbed.full_name is None
    assert scrubbed.phone is None


def test_purge_leaves_accounts_within_grace_period(db_session):
    within_grace = _make_deletion_requested_user(
        db_session, "recent@example.com", days_since_request=0,
    )

    purged_count = purge_expired_deletion_requests(db_session)

    assert purged_count == 0
    assert db_session.query(User).filter(User.id == within_grace.id).first() is not None


def test_purge_scrubs_addresses_without_an_n_plus_1_query_per_account(db_session):
    """Regression test: `for address in account.addresses` inside the purge
    loop used to lazy-load each account's addresses with a separate SELECT
    per account (classic N+1) - fine for one test account, not fine for a
    daily sweep over a real accounts table. purge_expired_deletion_requests
    now eager-loads addresses via selectinload(User.addresses), so the
    number of SELECTs stays flat (one extra query total) as the number of
    matched accounts grows."""
    accounts = [
        _make_deletion_requested_user(
            db_session, f"expired{i}@example.com",
            days_since_request=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1,
        )
        for i in range(5)
    ]
    for account in accounts:
        address = Address(
            user_id=account.id,
            street="123 Main Street",
            city="Springfield",
            state="Illinois",
            postal_code="62701",
            country="US",
            is_default=True,
        )
        db_session.add(address)
    db_session.commit()

    queries = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", _count)
    try:
        purged_count = purge_expired_deletion_requests(db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", _count)

    assert purged_count == 5
    # One SELECT for the accounts query, one for the eager-loaded addresses
    # (selectinload issues a single second query, not one per account) -
    # strictly less than one-per-account (5), which is what an N+1 would
    # produce.
    assert len(queries) <= 3, queries
    for account in accounts:
        scrubbed_address = (
            db_session.query(Address).filter(Address.user_id == account.id).first()
        )
        assert scrubbed_address.street == "REDACTED"


def test_purge_scrubs_every_trace_of_the_person(db_session):
    """Regression test (F-07): the purge skipped addresses the user had
    already deleted (soft delete, is_active=False), which kept their street,
    city and postal code, and never touched consent_history (the IP address
    and user agent of every consent given) or data_export_requests."""
    account = _make_deletion_requested_user(
        db_session, "gone@example.com",
        days_since_request=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1,
    )
    account.consent_history = [
        {"type": "gdpr", "granted": True, "timestamp": "2026-01-01T00:00:00",
         "ip_address": "203.0.113.7", "user_agent": "Mozilla/5.0 (Gone)"},
        {"type": "marketing", "granted": False, "timestamp": "2026-02-01T00:00:00",
         "ip_address": "198.51.100.9", "user_agent": "curl/8.0"},
    ]
    account.data_export_requests = [
        {"request_id": "export_1", "requested_at": "2026-03-01T00:00:00",
         "ip_address": "203.0.113.7"},
    ]
    # Different countries only because the test database is SQLite, where
    # uix_active_user_address is a plain unique index: on Postgres it is
    # partial (WHERE is_active), and two scrubbed, inactive addresses in the
    # same country do not collide.
    for street, country, is_active in (
        ("Old Street 5", "DE", False),
        ("New Street 7", "US", True),
    ):
        db_session.add(Address(
            user_id=account.id, street=street, city="Town", state="ST",
            postal_code="99999", country=country, is_active=is_active,
            delivery_phone="+15550000000", delivery_instructions="Ring twice",
        ))
    db_session.commit()

    assert purge_expired_deletion_requests(db_session) == 1

    db_session.expire_all()
    addresses = db_session.query(Address).filter(Address.user_id == account.id).all()
    assert len(addresses) == 2
    for address in addresses:
        assert (address.street, address.city, address.state, address.postal_code) == (
            "REDACTED", "REDACTED", "REDACTED", "REDACTED"
        )
        assert address.delivery_phone is None
        assert address.delivery_instructions is None
        assert address.is_active is False
    # Country stays: it is what tax records of past orders need.
    assert {a.country for a in addresses} == {"DE", "US"}

    scrubbed = db_session.get(User, account.id)
    # Art. 7(1): the fact, type and time of each consent stay as evidence;
    # who gave it (network address, browser) does not.
    assert scrubbed.consent_history == [
        {"type": "gdpr", "granted": True, "timestamp": "2026-01-01T00:00:00",
         "ip_address": None, "user_agent": None},
        {"type": "marketing", "granted": False, "timestamp": "2026-02-01T00:00:00",
         "ip_address": None, "user_agent": None},
    ]
    assert scrubbed.data_export_requests == []


def test_purge_ignores_active_accounts(db_session):
    # An active user has no deletion request at all - never eligible.
    active = User(
        email="active@example.com",
        hashed_password=get_password_hash("Str0ngPass1"),
        gdpr_consent=True,
        privacy_policy_accepted=True,
        is_active=True,
    )
    db_session.add(active)
    db_session.commit()
    db_session.refresh(active)

    purged_count = purge_expired_deletion_requests(db_session)

    assert purged_count == 0
    assert db_session.query(User).filter(User.id == active.id).first() is not None
