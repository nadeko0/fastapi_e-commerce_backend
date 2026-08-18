from enum import Enum

from sqlalchemy import String


# User related enums
class UserRole(str, Enum):
    CLIENT = "client"
    ADMIN = "admin"

# Address related enums
class AddressType(str, Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"

# Order related enums
class OrderStatus(str, Enum):
    NEW = "new"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SENT = "sent"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


def create_string_enum(enum_class, name):
    """Create a String column with check constraint for enum values"""
    values = [e.value for e in enum_class]
    return String, {'name': name, 'check': f"{name} IN {tuple(values)}"}
