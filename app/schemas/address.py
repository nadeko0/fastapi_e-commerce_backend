import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AddressType
from app.schemas.common import E164_PHONE_PATTERN, Int32, RequestBody, input_schema


# Mirrors app.schemas.user.UserBase's phone validation exactly - both
# represent the same "phone number" concept and previously diverged:
# UserBase.phone was validated (+digits, 5-15 digits) while
# AddressBase/AddressUpdate.delivery_phone accepted any string unvalidated.
def _validate_phone(v: str | None) -> str | None:
    if not v:
        return v
    phone = re.sub(r'\D', '', v)
    if not 5 <= len(phone) <= 15:
        raise ValueError('Invalid phone number length')
    return f"+{phone}"


def _read_phone(v: str | None) -> str | None:
    """Response schemas: a stored number is normalised when it can be and
    served as stored when it cannot (written before the rule, or edited in
    the database). A read never fails on what the database holds."""
    try:
        return _validate_phone(v)
    except ValueError:
        return v


def _normalise_input_country(v: str) -> str:
    """Input schemas only: the cased value is checked against the limit again,
    because the length limit runs before the normalisation and case mapping
    can lengthen a value ("ß".title() is "Ss")."""
    v = _normalise_country(v)
    if len(v) > 100:
        raise ValueError('String should have at most 100 characters')
    return v


def _validate_input_phone(v: str | None) -> str | None:
    """Input schemas only: the normalised number must also be ITU-T E.164.
    The response schemas keep _validate_phone so a number stored before this
    rule (for example "+0000000") still reads."""
    v = _validate_phone(v)
    if v and not re.fullmatch(E164_PHONE_PATTERN, v):
        raise ValueError('Invalid phone number format')
    return v


# What _validate_input_phone accepts, as a pattern for the OpenAPI document
# (not used to check a value): any text whose digits, 5 to 15 of them with the
# first not 0, make an E.164 number once everything else is dropped; or the
# empty string, which is passed through.
INPUT_PHONE_SCHEMA_PATTERN = r"^(\D*[1-9](\D*\d){4,14}\D*)?$"


def _normalise_country(v: str) -> str:
    """A two-letter code is upper-cased, anything else title-cased. Case
    mapping can change a length ("ß" upper-cases to "SS"), and the schema
    runs on input and again on every read of the stored row, so the rule has
    to give the same answer the second time: a value that upper-cases to more
    than two characters is title-cased in the same step, which is what the
    second pass would have done to it."""
    if len(v) == 2:
        v = v.upper()
        if len(v) == 2:
            return v
    return v.title()


class AddressBase(BaseModel):
    """An address as stored and as served. No length limits here: they are
    input rules (AddressCreate, AddressUpdate), and a stored row that breaks
    one must still be readable."""
    street: str
    city: str
    state: str
    postal_code: str
    country: str
    address_type: AddressType = Field(default=AddressType.HOME)
    is_default: bool = Field(default=False)
    delivery_instructions: str | None = None
    delivery_phone: str | None = None

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v):
        return ''.join(v.split())

    @field_validator('country')
    @classmethod
    def validate_country(cls, v):
        return _normalise_country(v)

    @field_validator('delivery_phone')
    @classmethod
    def validate_delivery_phone(cls, v):
        return _read_phone(v)

class AddressCreate(RequestBody, AddressBase):
    street: str = Field(..., min_length=5, max_length=255)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    postal_code: str = Field(..., min_length=3, max_length=20)
    country: str = Field(..., min_length=2, max_length=100)
    delivery_instructions: str | None = Field(None, max_length=500)

    model_config = ConfigDict(json_schema_extra=input_schema(
        rules={"delivery_phone": {"pattern": INPUT_PHONE_SCHEMA_PATTERN}},
    ))

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v):
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @field_validator('country')
    @classmethod
    def validate_country(cls, v):
        return _normalise_input_country(v)

    @field_validator('delivery_phone')
    @classmethod
    def validate_delivery_phone(cls, v):
        return _validate_input_phone(v)

class AddressUpdate(RequestBody):
    street: str | None = Field(None, min_length=5, max_length=255)
    city: str | None = Field(None, min_length=2, max_length=100)
    state: str | None = Field(None, min_length=2, max_length=100)
    postal_code: str | None = Field(None, min_length=3, max_length=20)
    country: str | None = Field(None, min_length=2, max_length=100)
    address_type: AddressType | None = None
    is_default: bool | None = None
    delivery_instructions: str | None = Field(None, max_length=500)
    delivery_phone: str | None = None

    # Everything may be left out; the NOT NULL columns may not be null.
    model_config = ConfigDict(json_schema_extra=input_schema(
        not_null=("street", "city", "state", "postal_code", "country", "address_type", "is_default"),
        rules={"delivery_phone": {"pattern": INPUT_PHONE_SCHEMA_PATTERN}},
    ))

    @field_validator('street', 'city', 'state', 'postal_code', 'country', 'address_type', 'is_default')
    @classmethod
    def reject_null(cls, v):
        """NOT NULL columns. delivery_instructions and delivery_phone are
        nullable: null clears them."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v):
        if v is None:
            return v
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @field_validator('country')
    @classmethod
    def validate_country(cls, v):
        if v is None:
            return v
        # Basic country code validation (can be enhanced with a proper country list)
        return _normalise_input_country(v)

    @field_validator('delivery_phone')
    @classmethod
    def validate_delivery_phone(cls, v):
        return _validate_input_phone(v)

class AddressResponse(AddressBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AddressListResponse(BaseModel):
    """Schema for list of addresses with pagination"""
    items: list[AddressResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class SetDefaultAddress(RequestBody):
    """Schema for setting an address as default"""
    address_id: Int32 = Field(..., description="ID of the address to set as default")
