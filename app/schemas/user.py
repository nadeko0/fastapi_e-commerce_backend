import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.clock import utc_now
from app.models.enums import UserRole
from app.schemas.common import E164_PHONE_PATTERN


class ConsentType(str, Enum):
    GDPR = "gdpr"
    MARKETING = "marketing"
    COOKIES = "cookies"
    PRIVACY_POLICY = "privacy_policy"



def normalize_email(email: str) -> str:
    """The one spelling of an email this application stores and compares:
    without surrounding blanks, in lower case. Case was significant before,
    so "Case@Example.com" and "case@example.com" became two accounts for
    one mailbox. Strictly the local part of an address may be case-
    sensitive (RFC 5321), but no mail provider in use treats it so, and
    two accounts for one mailbox are the worse problem. Lookups compare
    lower(email) as well, so rows stored before this keep working."""
    return email.strip().lower()


class ConsentHistory(BaseModel):
    type: ConsentType
    granted: bool
    timestamp: datetime
    ip_address: str | None = None
    user_agent: str | None = None

    model_config = ConfigDict(from_attributes=True)

class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = Field(None, min_length=2, max_length=100)
    phone: str | None = Field(None, pattern=r"^\+\d{5,15}$", description="International phone number (5-15 digits)")

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if not v:
            return v
        # Remove any non-digit characters
        phone = re.sub(r'\D', '', v)
        # Basic international phone validation (5-15 digits)
        if not 5 <= len(phone) <= 15:
            raise ValueError('Invalid phone number length')
        return f"+{phone}"

class UserCreate(UserBase):
    # Strict on input only: UserBase (shared with the responses) keeps the old
    # pattern so a number stored before the E.164 rule still reads.
    phone: str | None = Field(None, pattern=E164_PHONE_PATTERN, description="International phone number (5-15 digits)")

    password: str = Field(..., min_length=8, pattern=r"^[A-Za-z\d@$!%*#?&]{8,}$")

    @field_validator('email', mode="before")
    @classmethod
    def normalise_email(cls, v):
        return normalize_email(v) if isinstance(v, str) else v

    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v):
        """Validate password contains both letters and numbers"""
        if not any(c.isalpha() for c in v):
            raise ValueError('Password must contain at least one letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        # bcrypt hard-limits inputs to 72 bytes and raises instead of
        # truncating; reject oversized passwords here with a clean 422
        # rather than letting that surface as a 500 from the hasher.
        if len(v.encode('utf-8')) > 72:
            raise ValueError('Password must be at most 72 bytes long')
        return v
    gdpr_consent: bool = Field(..., description="GDPR consent is required")
    privacy_policy_accepted: bool = Field(..., description="Privacy policy must be accepted")
    marketing_consent: bool | None = Field(default=False)

    @field_validator('gdpr_consent')
    @classmethod
    def validate_gdpr_consent(cls, v):
        if not v:
            raise ValueError('GDPR consent is required')
        return v

    @field_validator('privacy_policy_accepted')
    @classmethod
    def validate_privacy_policy(cls, v):
        if not v:
            raise ValueError('Privacy policy must be accepted')
        return v

class UserUpdate(BaseModel):
    full_name: str | None = Field(None, min_length=2, max_length=100)
    phone: str | None = Field(None, pattern=E164_PHONE_PATTERN, description="International phone number (5-15 digits)")

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if not v:
            return v
        # Remove any non-digit characters
        phone = re.sub(r'\D', '', v)
        # Basic international phone validation (5-15 digits)
        if not 5 <= len(phone) <= 15:
            raise ValueError('Invalid phone number length')
        return f"+{phone}"
    marketing_consent: bool | None = None

    @field_validator('marketing_consent')
    @classmethod
    def reject_null(cls, v):
        """NOT NULL column. full_name and phone are nullable: null clears them."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

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
    last_login: datetime | None = None

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

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class LogoutRequest(BaseModel):
    refresh_token: str | None = None

class TokenPayload(BaseModel):
    sub: int  # user_id
    exp: datetime
    role: UserRole

class PasswordReset(BaseModel):
    email: EmailStr

    @field_validator('email', mode="before")
    @classmethod
    def normalise_email(cls, v):
        return normalize_email(v) if isinstance(v, str) else v

class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, pattern=r"^[A-Za-z\d@$!%*#?&]{8,}$")

    @field_validator('new_password')
    @classmethod
    def validate_new_password_strength(cls, v):
        """Validate new password contains both letters and numbers"""
        if not any(c.isalpha() for c in v):
            raise ValueError('Password must contain at least one letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one number')
        if len(v.encode('utf-8')) > 72:
            raise ValueError('Password must be at most 72 bytes long')
        return v

class GDPRExport(BaseModel):
    """Schema for GDPR data export metadata"""
    request_id: str = Field(..., description="Unique identifier for the export request")
    request_date: datetime = Field(..., description="When the export was requested")
    expires_at: datetime = Field(..., description="When the export data will be deleted")
    status: str = Field(default="processing", description="Current status of the export")

    model_config = ConfigDict(json_schema_extra={
            "examples": [{
                "request_id": "export_123_1642161234",
                "request_date": "2025-01-14T19:21:32.388Z",
                "expires_at": "2025-01-15T19:21:32.388Z",
                "status": "processing"
            }]
        })

class GDPRExportData(BaseModel):
    """Schema for the actual GDPR data export content"""
    personal_data: UserInDB
    consents: list[ConsentHistory]
    addresses: list["AddressResponse"]
    orders: list["OrderResponse"]
    export_metadata: GDPRExport
    generated_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(json_schema_extra={
            "examples": [{
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
            }]
        })

class GDPRDelete(BaseModel):
    """Schema for GDPR account deletion"""
    confirmation: bool = Field(..., description="Confirm account deletion")
    password: str
    reason: str | None = None

    @field_validator('confirmation')
    @classmethod
    def validate_confirmation(cls, v):
        if not v:
            raise ValueError('You must confirm account deletion')
        return v

# Circular imports are resolved at runtime
from app.schemas.address import AddressResponse  # noqa: E402
from app.schemas.order import OrderResponse  # noqa: E402
