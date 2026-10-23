from enum import Enum


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

