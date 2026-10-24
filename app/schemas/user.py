import re
import unicodedata
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.passwords import BLOCKED_MESSAGE, is_blocked_password, normalize_password
from app.models.enums import UserRole
from app.schemas.common import E164_PHONE_PATTERN, RequestBody


class ConsentType(str, Enum):
    GDPR = "gdpr"
    MARKETING = "marketing"
    COOKIES = "cookies"
    PRIVACY_POLICY = "privacy_policy"



def validate_new_password(v: str) -> str:
    """Rules for a password being set (registration, reset); never login.
    Returns the NFC form, which is what gets hashed."""
    v = normalize_password(v)
    if len(v) < 8:
        raise ValueError('Password must be at least 8 characters long')
    # bcrypt hard-limits inputs to 72 bytes and raises instead of
    # truncating; reject oversized passwords here with a clean 422
    # rather than letting that surface as a 500 from the hasher.
    if len(v.encode('utf-8')) > 72:
        raise ValueError('Password must be at most 72 bytes long')
    if is_blocked_password(v):
        raise ValueError(BLOCKED_MESSAGE)
    return v


def normalize_email(email: str) -> str:
    """The one spelling of an email this application stores and compares:
    without surrounding blanks, in lower case. Case was significant before,
    so "Case@Example.com" and "case@example.com" became two accounts for
    one mailbox. Strictly the local part of an address may be case-
    sensitive (RFC 5321), but no mail provider in use treats it so, and
    two accounts for one mailbox are the worse problem. Lookups compare
    lower(email) as well, so rows stored before this keep working.

    Unicode text with more than one spelling of the same characters
    ("e" + U+0301 and U+00E9) is composed to NFC first (UAX #15; RFC 6531
    section 3.3 asks for NFC in internationalised local parts), so those
    are one account too. Composing again after lower() covers the few
    letters whose lower case is a sequence. ASCII is unchanged."""
    composed = unicodedata.normalize("NFC", email.strip())
    return unicodedata.normalize("NFC", composed.lower())


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

class UserCreate(RequestBody, UserBase):
    # Strict on input only: UserBase (shared with the responses) keeps the old
    # pattern so a number stored before the E.164 rule still reads.
    phone: str | None = Field(None, pattern=E164_PHONE_PATTERN, description="International phone number (5-15 digits)")

    # No character-set or composition rule (NIST SP 800-63B-4 3.1.1.2); the
    # length floor is unchanged. Rules live in app.core.passwords.
    password: str = Field(..., min_length=8, description="At least 8 characters, any characters including spaces and Unicode; at most 72 bytes in UTF-8; not a commonly used password")

    @field_validator('email', mode="before")
    @classmethod
    def normalise_email(cls, v):
        return normalize_email(v) if isinstance(v, str) else v

    @field_validator('password')
    @classmethod
    def validate_password_strength(cls, v):
        return validate_new_password(v)

    @model_validator(mode="after")
    def password_not_derived_from_email(self):
        if is_blocked_password(self.password, self.email):
            raise ValueError(BLOCKED_MESSAGE)
        return self

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

class UserUpdate(RequestBody):
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

class RefreshTokenRequest(RequestBody):
    refresh_token: str

class LogoutRequest(RequestBody):
    refresh_token: str | None = None

class TokenPayload(BaseModel):
    sub: int  # user_id
    exp: datetime
    role: UserRole

class PasswordReset(RequestBody):
    email: EmailStr

    @field_validator('email', mode="before")
    @classmethod
    def normalise_email(cls, v):
        return normalize_email(v) if isinstance(v, str) else v

class TokenBody(RequestBody):
    """Body of the routes that take an emailed token (email verification,
    reset-token check): in the body rather than the URL, where it would end
    up in access logs, browser history and Referer."""
    token: str = Field(..., min_length=1)


class NewPassword(RequestBody):
    new_password: str = Field(..., min_length=8)

    @field_validator('new_password')
    @classmethod
    def validate_new_password_strength(cls, v):
        return validate_new_password(v)


class PasswordResetConfirm(TokenBody, NewPassword):
    """Body of the reset-by-token route: the token proves the right to set
    a password, so the current one is not sent. Both travel in the body:
    a token in the path would end up in access logs. A current_password
    (what clients sent before 4.0.0, when it was ignored) is refused like
    any other unknown member."""


class PasswordUpdate(NewPassword):
    current_password: str

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
