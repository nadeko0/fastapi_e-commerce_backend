"""
Database Models Package
"""
from app.models.address import Address
from app.models.base import Base
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product, ProductVariant
from app.models.user import User

# This ensures all models are imported and relationships can be properly established
__all__ = [
    'Base',
    'User',
    'Address',
    'Order',
    'OrderItem',
    'Product',
    'ProductVariant',
    'Category',
    'Payment',
    'WebhookEvent',
]
