import logging
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import String, cast, create_engine, func, literal, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, selectinload, sessionmaker

from app.core.clock import utc_now
from app.core.config import settings
from app.core.database import _build_connect_args
from app.core.security import get_password_hash
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderStatus, PaymentStatus
from app.services import stripe_events
from app.services.email import (
    send_low_stock_alert_email,
    send_order_cancellation_email,
    send_order_confirmation_email,
    send_order_status_update_email,
)
from app.services.orders import (
    UNPAID_PAYMENT_STATUSES,
    cancel_order_and_restock,
    is_unpaid_and_new,
)
from app.services.payment import get_payment_provider
from app.services.reconciliation import reconcile_payments
from app.services.redis import RedisService

logger = logging.getLogger(__name__)


def build_redis_broker_url(
    host: str, port: int, db: int, password: str | None = None
) -> str:
    """Build a redis:// broker URL, including auth when a password is
    configured. docker-compose.yml's redis service runs with
    --requirepass ${REDIS_PASSWORD} (app/services/redis.py's connection
    pool already authenticates with it), but this URL previously carried
    no credentials at all - a real worker pointed at that
    password-protected Redis would fail to connect. Local dev commonly
    runs Redis without a password, so an empty/None password must not
    produce a broken `redis://:@host:port/db` URL with a dangling empty
    auth segment - the `:<password>@` segment is only included when a
    password is actually set.

    The password is percent-encoded (`urllib.parse.quote`, safe="") before
    being embedded in the URL: it is an arbitrary operator-chosen secret
    (see .env.example / docker-compose's REDIS_PASSWORD), not guaranteed to
    be URL-safe. An unescaped `@`, `:`, `/`, `#`, or `%` in the password
    would otherwise be parsed as a URL delimiter (e.g. an `@` splitting the
    userinfo segment early) and either corrupt the host/port Celery connects
    to or silently truncate the password, rather than raising - a case
    Celery/kombu's own URL parser can't detect after the fact.
    """
    auth = f':{quote(password, safe="")}@' if password else ''
    return f'redis://{auth}{host}:{port}/{db}'


celery = Celery(
    'ecommerce_tasks',
    broker=build_redis_broker_url(
        settings.REDIS_HOST,
        settings.REDIS_PORT,
        settings.REDIS_DB,
        settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD is not None else None,
    ),
)

celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    worker_max_tasks_per_child=1000,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # With late acks a task that is still running when the broker connection
    # drops can no longer be acknowledged and the broker redelivers it, so it
    # would run twice, possibly in parallel. True cancels the stale run; the
    # redelivered copy is the one that counts. This is the default in Celery 6.0.
    worker_cancel_long_running_tasks_on_connection_loss=True,
    task_default_queue='default',
    task_queues={
        'default': {},
        'emails': {},
        'cleanup': {},
        'stats': {},
        'payments': {},
    },
    # Keyed by the tasks' registered names. Every task below is declared with
    # an explicit name= ("send_order_confirmation", not
    # "app.tasks.send_order_confirmation"), and Celery matches routes,
    # annotations and beat entries against that name, not the import path.
    # These used to be written with an "app.tasks." prefix and so matched
    # nothing: routing only worked because each task also sets queue= in its
    # decorator, and the rate limits were never applied at all.
    task_routes={
        'send_*': {'queue': 'emails'},
        'cleanup_*': {'queue': 'cleanup'},
        'update_*': {'queue': 'stats'},
        'check_*': {'queue': 'stats'},
    },
    # No glob patterns here: task_annotations is looked up by exact task
    # name (or '*' for all tasks), unlike task_routes.
    task_annotations={
        'send_order_confirmation': {'rate_limit': '100/m'},
        'send_order_status_update': {'rate_limit': '100/m'},
        'cleanup_inactive_accounts': {'rate_limit': '10/m'},
    },
    broker_transport_options={
        'visibility_timeout': 3600,
        # The webhook route publishes process_stripe_event while Stripe waits
        # for its answer: a broker that does not answer must fail fast (the
        # event is stored and the sweeper publishes it), not hang the request.
        'socket_connect_timeout': 5,
        # kombu's default is None (no limit): a broker that accepts the
        # connection and then never answers would hang the publisher just
        # the same. Well above the 1 s BRPOP poll the worker uses.
        'socket_timeout': 10,
    },
    task_time_limit=300,
    task_soft_time_limit=240,
)

# Celery has its own DB engine, separate from app/core/database.py's (the
# FastAPI app and the worker are different processes) - it must not skip the
# same idle_in_transaction_session_timeout/statement_timeout/pool_pre_ping
# defense-in-depth that engine has (see the long comment on
# _build_connect_args in app/core/database.py for why). A task that raises,
# times out (task_time_limit above), or is killed mid-transaction leaks a
# connection exactly the same way a cancelled request can - confirmed live:
# running this worker locally left 2 Postgres connections stuck permanently
# idle-in-transaction (still stuck 38+ minutes later) because this engine
# had no timeout protection at all.
engine = create_engine(
    settings.DATABASE_URI.get_secret_value(),
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE,
    echo=settings.SQL_DEBUG,
    connect_args=_build_connect_args(settings.DATABASE_URI.get_secret_value()),
)
SessionLocal = sessionmaker(autoflush=False, bind=engine)

@contextmanager
def task_session():
    # Every task must run its queries inside `with task_session() as db:`.
    # This replaced `db = next(get_db())` on a plain generator, which never
    # closed anything: the generator was discarded right after next(), and
    # the session's first query then opened a transaction that nothing ever
    # ended. Each task run left a connection "idle in transaction" until
    # Postgres killed it 30 s later (idle_in_transaction_session_timeout -
    # the defense described above was masking this, not preventing it).
    # Measured with a real worker: 14 task runs left 6 such connections, all
    # gone only after the 30 s timeout; with the context manager, none.
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# How long a "this email was already sent" marker (below) is kept in Redis.
# Must comfortably outlive Celery's own redelivery window
# (broker_transport_options['visibility_timeout'] above, 1 hour) plus the
# max_retries/retry_backoff schedule on these tasks, so a legitimate retry
# still sees the marker and skips resending; 7 days is generous slack for
# both while still expiring rather than growing the keyspace forever.
EMAIL_SENT_MARKER_TTL_SECONDS = 7 * 24 * 3600

@celery.task(
    name="send_order_confirmation",
    queue="emails",
    retry_backoff=True,
    max_retries=3,
    # Without autoretry_for, Celery's own max_retries/retry_backoff options
    # above do nothing at all: they only configure what happens when the
    # task body calls self.retry() (which requires bind=True) - a plain,
    # unbound task that simply raises is marked FAILURE and acked (this task
    # doesn't set bind=True, so it has no `self` to call .retry() on). That
    # made this look like it retried transient DB hiccups when it never did.
    # Scoped to OperationalError (the DB connectivity/timeout exception
    # class) rather than all exceptions, so a genuine bug in this task still
    # fails fast instead of being retried 3 times first.
    autoretry_for=(OperationalError,),
)
def send_order_confirmation(order_id: int) -> None:
    # Celery's at-least-once delivery (task_acks_late=True +
    # task_reject_on_worker_lost=True, set above) means this task can be
    # redelivered and re-run for the same order_id - a worker killed after
    # the email was already sent but before the broker recorded the ack, or
    # (now that autoretry_for is wired up) a genuine retry after a transient
    # OperationalError raised partway through. Without a dedup guard, the
    # customer would receive the same "Order Confirmation" email twice.
    # mark_once claims the marker atomically in Redis before sending, so a
    # redelivered/retried task sees its own prior success and skips the
    # resend instead of sending it again.
    #
    # The marker is claimed only after every read the email needs: the
    # order, its user and its items' products are loaded up front instead
    # of lazily inside the send. It used to be claimed before the order was
    # even queried, so a transient OperationalError on any of those reads -
    # the very error autoretry_for retries - left the marker set, the retry
    # skipped the send, and the customer never got the email.
    with task_session() as db:
        order = db.scalars(
            select(Order)
            .options(
                joinedload(Order.user),
                selectinload(Order.items).joinedload(OrderItem.product),
            )
            .where(Order.id == order_id)
        ).first()
        if not order:
            return
        if not RedisService().mark_once(
            f"email_sent:order_confirmation:{order_id}", EMAIL_SENT_MARKER_TTL_SECONDS
        ):
            return
        # Everything the email needs is loaded: end the transaction before
        # the (possibly slow) SMTP call, not after it.
        db.expunge_all()
        db.rollback()
        send_order_confirmation_email(order.user.email, order)

@celery.task(
    name="send_order_status_update",
    queue="emails",
    retry_backoff=True,
    max_retries=3,
    autoretry_for=(OperationalError,),
)
def send_order_status_update(order_id: int) -> None:
    with task_session() as db:
        # joinedload(Order.user): the recipient's address must be read before
        # the marker below is claimed, not lazily after it (see
        # send_order_confirmation above for what a failed read did then).
        order = db.scalars(
            select(Order).options(joinedload(Order.user)).where(Order.id == order_id)
        ).first()
        if not order:
            return
        # Keyed by status (not just order_id): unlike the one-shot confirmation
        # email above, this task legitimately fires again every time the order's
        # status changes - only a redelivery/retry of the *same* status update
        # should be deduped. order.status is a plain str here (Order.status is a
        # bare String column, not a SQLAlchemy Enum type - see
        # app/models/order.py) rather than an OrderStatus
        # member, unlike OrderResponse.status (a Pydantic model, which does
        # coerce it to the enum) - handle both shapes rather than assuming
        # `.value` is always present.
        status_value = order.status.value if hasattr(order.status, "value") else order.status
        if not RedisService().mark_once(
            f"email_sent:order_status_update:{order_id}:{status_value}",
            EMAIL_SENT_MARKER_TTL_SECONDS,
        ):
            return
        # Loaded above: end the transaction before the SMTP call.
        db.expunge_all()
        db.rollback()
        send_order_status_update_email(order.user.email, order)

# Accounts anonymised per transaction by purge_expired_deletion_requests.
# Each account costs a bcrypt hash (BCRYPT_ROUNDS, ~0.25 s at 12), so a
# batch of 100 is ~25 s: well inside task_soft_time_limit, and a run cut
# short by it keeps every batch committed before.
PURGE_BATCH_SIZE = 100

def purge_expired_deletion_requests(db, batch_size: int = PURGE_BATCH_SIZE) -> int:
    """
    Anonymizes accounts whose GDPR Article 17 erasure request has passed
    the grace period. This only targets users who explicitly requested
    deletion (data_deletion_requested=True) - it is not a general inactivity
    purge, so it uses DATA_DELETION_GRACE_PERIOD_DAYS (default: 1 day, to
    match what users are told at request time), not
    INACTIVE_ACCOUNT_DELETE_DAYS.

    Scrubs PII rather than hard-deleting the User row: User.orders and
    Address.user both cascade="all, delete-orphan", so db.delete(user)
    would also wipe the user's order/invoice history. Most EU member
    states require invoices retained ~10 years for tax purposes - Article
    17(3)(b) exempts data still needed for a legal obligation from
    erasure, so financial records must survive account erasure while the
    personal identifiers on them do not. Anonymizing in place keeps
    Order/OrderItem/Address rows (and the Order.shipping_address_id FK)
    intact for that retention requirement while removing anything that
    identifies the person.

    Extracted from the cleanup_inactive_accounts task so it can be unit
    tested against a session without going through Celery/the real DB.
    """
    grace_cutoff = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS)

    # selectinload(User.addresses): without it, `for address in
    # account.addresses` below lazy-loads addresses with one SELECT per
    # account (classic N+1) - fine for the single-user regression tests, but
    # this runs as a daily sweep over every account past the grace period,
    # so it does not stay fine at any real scale. selectinload issues one
    # extra query total (IN (...) over all matched account ids) instead of
    # one per account.
    #
    # Accounts already anonymised are excluded: data_deletion_requested is
    # never reset, and without this every daily run re-scrubbed and
    # re-hashed every account erased since the service went live, in one
    # transaction, until a run no longer fit task_soft_time_limit and
    # rolled back - every day, silently, with no new erasure getting
    # through. The marker is the email this function writes, matched
    # exactly (id included), so no column or migration is needed.
    anonymised_email = (
        literal("deleted-user-") + cast(User.id, String) + literal("@deleted.invalid")
    )
    pending = select(User).options(selectinload(User.addresses)).where(
        User.is_active == False,  # noqa: E712 - `not User.is_active` evaluates in
        # Python immediately instead of building a SQL clause and silently
        # produces an always-false filter; explicit `== False` is required.
        User.data_deletion_requested,
        User.data_deletion_date <= grace_cutoff,
        User.email != anonymised_email,
    ).order_by(User.id)

    return _anonymise_pending(db, pending, batch_size)

def _anonymise_pending(db, pending, batch_size: int, due=None) -> int:
    """Anonymises, batch by batch, the accounts `pending` selects (ordered by
    id), erases the stored Stripe events about their payments, and commits
    after each batch."""
    purged_count = 0
    last_id = 0
    while True:
        # Keyset pagination on id, not just "the next unscrubbed rows": a
        # row that somehow stayed unscrubbed is not picked up again, so the
        # loop always ends.
        accounts = db.scalars(pending.where(User.id > last_id).limit(batch_size)).all()
        if not accounts:
            break
        last_id = accounts[-1].id
        erased = [a for a in accounts if due is None or due(a)]
        for account in erased:
            _anonymise_account(account)
        stripe_events.redact_users_events(db, [a.id for a in erased])
        purged_count += len(erased)
        db.commit()
        if len(accounts) < batch_size:
            break
    return purged_count

def _anonymise_account(account) -> None:
    account.email = f"deleted-user-{account.id}@deleted.invalid"
    account.hashed_password = get_password_hash(str(uuid4()))
    account.full_name = None
    account.phone = None
    account.marketing_consent = False
    # Art. 7(1) evidence stays (type, granted, timestamp); who gave the
    # consent - network address and browser - does not. Reassigned, not
    # mutated in place: SQLAlchemy does not see in-place changes to a
    # plain JSON column.
    account.consent_history = [
        {**entry, "ip_address": None, "user_agent": None}
        for entry in account.consent_history or []
    ]
    account.data_export_requests = []

    # Every address, including the ones the user deleted earlier (soft
    # delete, is_active=False): those used to be skipped and kept their
    # street, city and postal code. Country stays for the tax records
    # of past orders.
    for address in account.addresses:
        address.street = "REDACTED"
        address.city = "REDACTED"
        address.state = "REDACTED"
        address.postal_code = "REDACTED"
        address.delivery_phone = None
        address.delivery_instructions = None
        address.is_active = False

@celery.task(
    name="cleanup_inactive_accounts",
    queue="cleanup",
)
def cleanup_inactive_accounts() -> int:
    with task_session() as db:
        return purge_expired_deletion_requests(db)

def purge_expired_retention(db, batch_size: int = PURGE_BATCH_SIZE) -> int:
    """
    Applies USER_DATA_RETENTION_DAYS (GDPR Art. 5(1)(e), storage limitation).
    The privacy policy (GET /legal/privacy-policy, section 6) says "we keep
    your data for N days unless required longer by law, you request
    deletion, or there are legal proceedings"; it does not say from when,
    and keeping a live customer's account for N days from sign-up would
    delete everyone. The narrowest reading is applied: an account that is
    deactivated (is_active false: closed, not merely quiet) and has shown no
    activity (last_login, last_activity_date, which any change to the row
    refreshes) for the user's own data_retention_period days (the setting's
    value when unset) is anonymised exactly like an erasure request:
    personal identifiers go, orders and invoices stay (Art. 17(3)(b)), and
    the stored Stripe events about its payments are redacted. Active
    accounts and administrators are never touched. Accounts already
    anonymised, or being erased through a request, are left to
    purge_expired_deletion_requests / skipped. Idempotent; commits per
    batch.
    """
    anonymised_email = (
        literal("deleted-user-") + cast(User.id, String) + literal("@deleted.invalid")
    )
    pending = select(User).options(selectinload(User.addresses)).where(
        User.is_active == False,  # noqa: E712 - see purge_expired_deletion_requests
        User.role != "admin",
        User.email != anonymised_email,
    ).order_by(User.id)
    now = utc_now()

    def retention_has_passed(account) -> bool:
        # The period is per user (their own data_retention_period, the
        # setting's value when unset), so the comparison is made here, not in
        # SQL: the candidates are only the deactivated, not yet anonymised
        # accounts, and every purge shrinks that set.
        days = account.data_retention_period or settings.USER_DATA_RETENTION_DAYS
        last_seen = max(
            moment for moment in (
                account.last_login, account.last_activity_date, account.created_at
            ) if moment is not None
        )
        return now - last_seen > timedelta(days=days)

    purged = _anonymise_pending(db, pending, batch_size, retention_has_passed)
    logger.info("Retention purge anonymised %d account(s)", purged)
    return purged

@celery.task(
    name="cleanup_expired_retention",
    queue="cleanup",
)
def cleanup_expired_retention() -> int:
    with task_session() as db:
        return purge_expired_retention(db)

def expire_unpaid_orders(db) -> int:
    """
    Cancels every order still new and unpaid UNPAID_ORDER_EXPIRE_MINUTES
    after it was placed, returning its stock through the same code as an
    admin's cancellation (cancel_order_and_restock). Stock is taken when an
    order is placed, so without this an order nobody pays for keeps its
    items out of sale until someone cancels it by hand.

    Each order is locked (FOR UPDATE SKIP LOCKED: one a request is working
    on right now is left for the next run), re-checked under the lock, and
    committed on its own, so a customer's cancel, an admin's, or a payment
    that lands at the same moment is never undone or doubled. A payment that
    completes after its order expired is flagged for manual review by the
    payment code, not applied. The customer is told by email.

    Extracted from the cancel_expired_unpaid_orders task so it can be tested
    against a session without Celery, like purge_expired_deletion_requests.
    """
    cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
    candidates = list(
        db.scalars(
            select(Order.id).where(
                Order.status == OrderStatus.NEW,
                Order.payment_status.in_([s.value for s in UNPAID_PAYMENT_STATUSES]),
                Order.created_at < cutoff,
            )
        )
    )
    db.commit()

    cancelled = 0
    for order_id in candidates:
        order = db.scalars(
            select(Order).where(Order.id == order_id).with_for_update(skip_locked=True)
        ).first()
        if order is None or not is_unpaid_and_new(order) or order.created_at >= cutoff:
            db.rollback()
            continue
        cancel_order_and_restock(db, order)
        # What the email needs is read before the commit: after it the
        # attributes are expired, and reading order.user would open a new
        # transaction that stays open through the SMTP call (a slow server
        # past idle_in_transaction_session_timeout ends the connection and
        # aborts the sweep).
        recipient = order.user.email
        notice = SimpleNamespace(
            id=order.id,
            status=order.status,
            payment_status=order.payment_status,
            created_at=order.created_at,
            total_amount=order.total_amount,
            currency=order.currency,
        )
        db.commit()
        cancelled += 1
        send_order_cancellation_email(recipient, notice)
    return cancelled

@celery.task(
    name="cancel_expired_unpaid_orders",
    queue="cleanup",
)
def cancel_expired_unpaid_orders() -> int:
    with task_session() as db:
        return expire_unpaid_orders(db)

@celery.task(
    name="check_payment_reconciliation",
    queue="stats",
)
def check_payment_reconciliation(order_id: int | None = None) -> dict:
    """Compare our payments with the provider's and record the differences
    (app/services/reconciliation.py). Daily from beat; an admin can queue
    it, or run it for one order, through POST /admin/reconciliation/run."""
    with task_session() as db:
        return reconcile_payments(db, get_payment_provider(), order_id=order_id).as_dict()

@celery.task(
    bind=True,
    name="process_stripe_event",
    queue="payments",
    # No limit of Celery's own: the number of retries is counted on the
    # event (WebhookEvent.attempts, STRIPE_EVENT_MAX_RETRIES), where an
    # admin sees it and a replay resets it.
    max_retries=None,
)
def process_stripe_event(self, event_id: str) -> dict:
    """Apply a stored Stripe event (see stripe_events.process_stored_event).
    The webhook route publishes it after storing the event, the sweeper
    publishes it for an event whose message was lost, and it publishes
    itself again, with a backoff, for an event it could not apply."""
    with task_session() as db:
        outcome = stripe_events.process_stored_event(db, event_id)
    if outcome.retry_in is not None:
        raise self.retry(countdown=outcome.retry_in)
    return outcome.result or {"status": outcome.state}

@celery.task(
    name="sweep_stripe_events",
    queue="payments",
)
def sweep_stripe_events() -> int:
    with task_session() as db:
        return stripe_events.enqueue_stuck_events(db)

@celery.task(
    name="update_product_stats",
    queue="stats",
)
def update_product_stats() -> None:
    redis = RedisService()

    with task_session() as db:
        popular_products = db.execute(
            select(
                Product.id,
                Product.name,
                func.sum(OrderItem.quantity).label('total_quantity'),
                func.sum(OrderItem.quantity * OrderItem.price_at_time).label('total_revenue')
            )
            .select_from(Product)
            .join(OrderItem)
            .join(Order)
            .where(
                Order.created_at >= utc_now() - timedelta(days=30),
                Order.status != OrderStatus.CANCELLED,
                Order.payment_status == PaymentStatus.PAID
            )
            .group_by(Product.id)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(20)
        ).all()

    stats = {
        "popular_products": [
            {
                "id": p.id,
                "name": p.name,
                "total_quantity": p.total_quantity,
                "total_revenue": float(p.total_revenue)
            }
            for p in popular_products
        ],
        "updated_at": utc_now().isoformat()
    }

    # RedisService has no set_product_stats method (never did) - this
    # always raised AttributeError, so update_product_stats has never
    # completed successfully. Use the generic setex it does provide,
    # matching the caching pattern already used by admin.get_statistics.
    redis.setex("product_stats", 3600, stats)

@celery.task(
    name="check_low_stock",
    queue="stats",
)
def check_low_stock(threshold: int = 5) -> None:
    with task_session() as db:
        low_stock_products = db.scalars(
            select(Product).where(
                Product.is_active,
                Product.stock_quantity <= threshold,
            )
        ).all()

        # Loaded: end the transaction before the SMTP call.
        db.expunge_all()
        db.rollback()
        if low_stock_products:
            send_low_stock_alert_email(
                settings.ADMIN_EMAIL,
                low_stock_products
            )

# 'task' is the registered task name (see the note on task_routes above).
# With the "app.tasks." prefix these entries used to carry, beat published
# messages for four tasks no worker has: the worker logs "Received
# unregistered task" and drops them, so none of the periodic jobs ever ran.
#
# No cart sweep: every cart write is a SETEX, so Redis expires cart and
# guest-cart keys itself. The hourly cleanup_expired_carts task sent one TTL
# request per key looking for keys without an expiry, which nothing creates.
celery.conf.beat_schedule = {
    # Daily at a fixed time, not timedelta(days=1): that counts from the
    # start of beat, so every container re-create moved the next run a full
    # day back. 02:30 keeps clear of the 03:15 reconciliation.
    'cleanup-inactive-accounts': {
        'task': 'cleanup_inactive_accounts',
        'schedule': crontab(hour=2, minute=30),
    },
    'update-product-stats': {
        'task': 'update_product_stats',
        'schedule': timedelta(hours=1),
    },
    'check-low-stock': {
        'task': 'check_low_stock',
        'schedule': timedelta(hours=4),
    },
    # Every 5 minutes, so an unpaid order holds its stock for at most
    # UNPAID_ORDER_EXPIRE_MINUTES plus 5.
    'cancel-expired-unpaid-orders': {
        'task': 'cancel_expired_unpaid_orders',
        'schedule': timedelta(minutes=5),
    },
    # Once a day, at a quiet hour, so a lost webhook or a Dashboard refund is
    # found by the next morning.
    'check-payment-reconciliation': {
        'task': 'check_payment_reconciliation',
        'schedule': crontab(hour=3, minute=15),
    },
    # Publishes the Stripe events whose task message was lost (the broker was
    # down when the webhook route stored them); see STRIPE_EVENT_SWEEP_*.
    'sweep-stripe-events': {
        'task': 'sweep_stripe_events',
        'schedule': timedelta(minutes=5),
    },
    # USER_DATA_RETENTION_DAYS (GDPR Art. 5(1)(e)); 02:45, between the
    # erasure purge (02:30) and the reconciliation (03:15).
    'cleanup-expired-retention': {
        'task': 'cleanup_expired_retention',
        'schedule': crontab(hour=2, minute=45),
    },
}
