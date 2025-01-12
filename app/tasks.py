from datetime import datetime, timedelta
from typing import List, Dict, Any
from celery import Celery
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from decimal import Decimal

from app.core.config import settings
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

@celery.task(
    name="cleanup_inactive_accounts",
    queue="cleanup",
)
def cleanup_inactive_accounts() -> int:
    db = next(get_db())
    retention_date = datetime.utcnow() - timedelta(days=settings.INACTIVE_ACCOUNT_DELETE_DAYS)
    
    accounts = db.query(User).filter(
        User.is_active == False,
        User.data_deletion_requested == True,
        User.data_deletion_date <= retention_date
    ).all()

    deleted_count = 0
    for account in accounts:
        db.delete(account)
        deleted_count += 1

    db.commit()
    return deleted_count

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

    redis.set_product_stats(stats)

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
            Product.stock_quantity <= threshold
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