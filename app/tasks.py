from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

from celery import Celery
from sqlalchemy import create_engine, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload, sessionmaker

from app.core.config import settings
from app.core.database import _build_connect_args
from app.core.security import get_password_hash
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.email import (
    send_low_stock_alert_email,
    send_order_confirmation_email,
    send_order_status_update_email,
)
from app.services.redis import RedisService


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
        settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB, settings.REDIS_PASSWORD
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
    task_default_queue='default',
    task_queues={
        'default': {},
        'emails': {},
        'cleanup': {},
        'stats': {},
    },
    task_routes={
        'app.tasks.send_*': {'queue': 'emails'},
        'app.tasks.cleanup_*': {'queue': 'cleanup'},
        'app.tasks.update_*': {'queue': 'stats'},
    },
    task_annotations={
        'app.tasks.send_*': {'rate_limit': '100/m'},
        'app.tasks.cleanup_*': {'rate_limit': '10/m'},
    },
    broker_transport_options={
        'visibility_timeout': 3600,
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
    settings.DATABASE_URI,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=3600,
    connect_args=_build_connect_args(settings.DATABASE_URI),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
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
    if not RedisService().mark_once(
        f"email_sent:order_confirmation:{order_id}", EMAIL_SENT_MARKER_TTL_SECONDS
    ):
        return
    db = next(get_db())
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        send_order_confirmation_email(order.user.email, order)

@celery.task(
    name="send_order_status_update",
    queue="emails",
    retry_backoff=True,
    max_retries=3,
    autoretry_for=(OperationalError,),
)
def send_order_status_update(order_id: int) -> None:
    db = next(get_db())
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return
    # Keyed by status (not just order_id): unlike the one-shot confirmation
    # email above, this task legitimately fires again every time the order's
    # status changes - only a redelivery/retry of the *same* status update
    # should be deduped. order.status is a plain str here (Order.status is a
    # bare String column, not a SQLAlchemy Enum type - see
    # app/models/enums.py's create_string_enum) rather than an OrderStatus
    # member, unlike OrderResponse.status (a Pydantic model, which does
    # coerce it to the enum) - handle both shapes rather than assuming
    # `.value` is always present.
    status_value = order.status.value if hasattr(order.status, "value") else order.status
    if not RedisService().mark_once(
        f"email_sent:order_status_update:{order_id}:{status_value}",
        EMAIL_SENT_MARKER_TTL_SECONDS,
    ):
        return
    send_order_status_update_email(order.user.email, order)

@celery.task(
    name="cleanup_expired_carts",
    queue="cleanup",
)
def cleanup_expired_carts() -> int:
    redis = RedisService()
    return redis.cleanup_expired_carts()

def purge_expired_deletion_requests(db) -> int:
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
    grace_cutoff = datetime.utcnow() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS)

    # selectinload(User.addresses): without it, `for address in
    # account.addresses` below lazy-loads addresses with one SELECT per
    # account (classic N+1) - fine for the single-user regression tests, but
    # this runs as a daily sweep over every account past the grace period,
    # so it does not stay fine at any real scale. selectinload issues one
    # extra query total (IN (...) over all matched account ids) instead of
    # one per account.
    accounts = db.query(User).options(selectinload(User.addresses)).filter(
        User.is_active == False,  # noqa: E712 - `not User.is_active` evaluates in
        # Python immediately instead of building a SQL clause and silently
        # produces an always-false filter; explicit `== False` is required.
        User.data_deletion_requested,
        User.data_deletion_date <= grace_cutoff
    ).all()

    purged_count = 0
    for account in accounts:
        account.email = f"deleted-user-{account.id}@deleted.invalid"
        account.hashed_password = get_password_hash(str(uuid4()))
        account.full_name = None
        account.phone = None
        account.marketing_consent = False

        for address in account.addresses:
            if not address.is_active:
                continue
            address.street = "REDACTED"
            address.city = "REDACTED"
            address.state = "REDACTED"
            address.postal_code = "REDACTED"
            address.delivery_phone = None
            address.delivery_instructions = None
            address.is_active = False

        purged_count += 1

    db.commit()
    return purged_count

@celery.task(
    name="cleanup_inactive_accounts",
    queue="cleanup",
)
def cleanup_inactive_accounts() -> int:
    db = next(get_db())
    return purge_expired_deletion_requests(db)

@celery.task(
    name="update_product_stats",
    queue="stats",
)
def update_product_stats() -> None:
    db = next(get_db())
    redis = RedisService()

    popular_products = (
        db.query(
            Product.id,
            Product.name,
            func.sum(OrderItem.quantity).label('total_quantity'),
            func.sum(OrderItem.quantity * OrderItem.price_at_time).label('total_revenue')
        )
        .join(OrderItem)
        .join(Order)
        .filter(
            Order.created_at >= datetime.utcnow() - timedelta(days=30),
            Order.status != OrderStatus.CANCELLED,
            Order.payment_status == PaymentStatus.PAID
        )
        .group_by(Product.id)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(20)
        .all()
    )

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
        "updated_at": datetime.utcnow().isoformat()
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
    db = next(get_db())

    low_stock_products = (
        db.query(Product)
        .filter(
            Product.is_active,
            Product.stock_quantity <= threshold,
        )
        .all()
    )

    if low_stock_products:
        send_low_stock_alert_email(
            settings.ADMIN_EMAIL,
            low_stock_products
        )

celery.conf.beat_schedule = {
    'cleanup-expired-carts': {
        'task': 'app.tasks.cleanup_expired_carts',
        'schedule': timedelta(hours=1),
    },
    'cleanup-inactive-accounts': {
        'task': 'app.tasks.cleanup_inactive_accounts',
        'schedule': timedelta(days=1),
    },
    'update-product-stats': {
        'task': 'app.tasks.update_product_stats',
        'schedule': timedelta(hours=1),
    },
    'check-low-stock': {
        'task': 'app.tasks.check_low_stock',
        'schedule': timedelta(hours=4),
    },
}
