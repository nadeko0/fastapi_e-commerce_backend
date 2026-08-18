from datetime import datetime, timedelta
from typing import List, Dict, Any
from uuid import uuid4
from celery import Celery
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from decimal import Decimal

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User
from app.models.order import Order
from app.models.product import Product
from app.models.order_items import OrderItem
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.redis import RedisService
from app.services.email import (
    send_order_confirmation_email,
    send_order_status_update_email,
    send_order_cancellation_email,
    send_low_stock_alert_email,
)

celery = Celery(
    'ecommerce_tasks',
    broker=f'redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}'
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

engine = create_engine(settings.DATABASE_URI)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@celery.task(
    name="send_order_confirmation",
    queue="emails",
    retry_backoff=True,
    max_retries=3,
)
def send_order_confirmation(order_id: int) -> None:
    db = next(get_db())
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        send_order_confirmation_email(order.user.email, order)

@celery.task(
    name="send_order_status_update",
    queue="emails",
    retry_backoff=True,
    max_retries=3,
)
def send_order_status_update(order_id: int) -> None:
    db = next(get_db())
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
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

    accounts = db.query(User).filter(
        User.is_active == False,
        User.data_deletion_requested == True,
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
            Product.is_active == True,
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