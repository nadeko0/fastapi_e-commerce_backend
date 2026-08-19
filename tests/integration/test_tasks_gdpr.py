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
