from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict, validator
from enum import Enum
import re

class UserRole(str, Enum):
    CLIENT = "client"
    ADMIN = "admin"

class ConsentType(str, Enum):
    GDPR = "gdpr"
    MARKETING = "marketing"
    COOKIES = "cookies"
    PRIVACY_POLICY = "privacy_policy"



class ConsentHistory(BaseModel):
    type: ConsentType
    granted: bool
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    phone: Optional[str] = Field(None, pattern=r"^\+\d{5,15}$", description="International phone number (5-15 digits)")

    @validator('phone')
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        # Remove any non-digit characters
        phone = re.sub(r'\D', '', v)
        # Basic international phone validation (5-15 digits)
        if not 5 <= len(phone) <= 15:
            raise ValueError('Invalid phone number length')
        return f"+{phone}"

class UserCreate(UserBase):
    password: str = Field(..., min_length=8, pattern=r"^[A-Za-z\d@$!%*#?&]{8,}$")

    @validator('password')
    def validate_password_strength(cls, v):
        """Validate password contains both letters and numbers"""
        if not any(c.isalpha() for c in v):
            raise ValueError('Password must contain at least one letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        return v
    gdpr_consent: bool = Field(..., description="GDPR consent is required")
    privacy_policy_accepted: bool = Field(..., description="Privacy policy must be accepted")
    marketing_consent: Optional[bool] = Field(default=False)

    @validator('gdpr_consent')
    def validate_gdpr_consent(cls, v):
        if not v:
            raise ValueError('GDPR consent is required')
        return v

    @validator('privacy_policy_accepted')
    def validate_privacy_policy(cls, v):
        if not v:
            raise ValueError('Privacy policy must be accepted')
        return v

class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    phone: Optional[str] = Field(None, pattern=r"^\+\d{5,15}$", description="International phone number (5-15 digits)")

    @validator('phone')
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        # Remove any non-digit characters
        phone = re.sub(r'\D', '', v)
        # Basic international phone validation (5-15 digits)
        if not 5 <= len(phone) <= 15:
            raise ValueError('Invalid phone number length')
        return f"+{phone}"
    marketing_consent: Optional[bool] = None

class UserInDB(UserBase):
    id: int
    role: UserRole
    is_active: bool
    is_email_verified: bool
    gdpr_consent: bool
    privacy_policy_accepted: bool
    marketing_consent: bool
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class UserResponse(UserBase):
    id: int
    role: UserRole
    is_active: bool
    is_email_verified: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class TokenPayload(BaseModel):
    sub: int  # user_id
    exp: datetime
    role: UserRole

class PasswordReset(BaseModel):
    email: EmailStr

class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, pattern=r"^[A-Za-z\d@$!%*#?&]{8,}$")

    @validator('new_password')
    def validate_new_password_strength(cls, v):
        """Validate new password contains both letters and numbers"""
        if not any(c.isalpha() for c in v):
            raise ValueError('Password must contain at least one letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        return v

class GDPRExport(BaseModel):
    """Schema for GDPR data export metadata"""
    request_id: str = Field(..., description="Unique identifier for the export request")
    request_date: datetime = Field(..., description="When the export was requested")
    expires_at: datetime = Field(..., description="When the export data will be deleted")
    status: str = Field(default="processing", description="Current status of the export")
    
    class Config:
        json_schema_extra = {
            "example": {
                "request_id": "export_123_1642161234",
                "request_date": "2025-01-14T19:21:32.388Z",
                "expires_at": "2025-01-15T19:21:32.388Z",
                "status": "processing"
            }
        }

class GDPRExportData(BaseModel):
    """Schema for the actual GDPR data export content"""
    personal_data: UserInDB
    consents: List[ConsentHistory]
    addresses: List["AddressResponse"]
    orders: List["OrderResponse"]
    export_metadata: GDPRExport
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_schema_extra = {
            "example": {
                "personal_data": {},
                "consents": [],
                "addresses": [],
                "orders": [],
                "export_metadata": {
                    "request_id": "export_123_1642161234",
                    "request_date": "2025-01-14T19:21:32.388Z",
                    "expires_at": "2025-01-15T19:21:32.388Z",
                    "status": "completed"
                },
                "generated_at": "2025-01-14T19:21:32.388Z"
            }
        }

class GDPRDelete(BaseModel):
    """Schema for GDPR account deletion"""
    confirmation: bool = Field(..., description="Confirm account deletion")
    password: str
    reason: Optional[str] = None

    @validator('confirmation')
    def validate_confirmation(cls, v):
        if not v:
            raise ValueError('You must confirm account deletion')
        return v

# Circular imports are resolved at runtime
from app.schemas.address import AddressResponse  # noqa: E402
from app.schemas.order import OrderResponse  # noqa: E402