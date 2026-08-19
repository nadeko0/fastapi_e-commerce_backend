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
    """Return the String column type used to store `enum_class` values.

    Every caller (app/models/user.py, order.py, address.py) pairs this with
    its own explicit CheckConstraint in __table_args__ enforcing the same
    enum_class values, so this only needs to hand back the column type -
    it previously also returned a {'name', 'check'} dict that no caller
    ever consumed (all four call sites sliced it off with `[0:1]`), which
    was dead, misleading code.
    """
    return String
