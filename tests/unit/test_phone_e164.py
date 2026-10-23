"""Phone numbers follow ITU-T E.164: '+', a country code that never starts
with 0, at most 15 digits in total. Input schemas are strict; response
schemas stay lenient so a value stored before the rule still reads."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.address import AddressCreate, AddressResponse, AddressUpdate
from app.schemas.user import UserCreate, UserResponse, UserUpdate

_ADDRESS = {
    "street": "1 Main Street",
    "city": "Berlin",
    "state": "Berlin",
    "postal_code": "10115",
    "country": "DE",
}
_USER = {
    "email": "phone@example.com",
    "password": "Passw0rd123",
    "gdpr_consent": True,
    "privacy_policy_accepted": True,
}

VALID = ["+14155552671", "+442071838750", "+79161234567", "+12345", "+" + "1" * 15]
LEADING_ZERO = ["+0000000", "+0123456789"]
TOO_LONG = ["+" + "1" * 16, "+1234567890123456789"]


def _user_create(phone):
    return UserCreate(**_USER, phone=phone)


def _address_create(phone):
    return AddressCreate(**_ADDRESS, delivery_phone=phone)


@pytest.mark.parametrize("phone", VALID)
def test_valid_e164_numbers_are_accepted_unchanged(phone):
    assert _user_create(phone).phone == phone
    assert UserUpdate(phone=phone).phone == phone
    assert _address_create(phone).delivery_phone == phone
    assert AddressUpdate(delivery_phone=phone).delivery_phone == phone


@pytest.mark.parametrize("phone", LEADING_ZERO + TOO_LONG)
def test_user_schemas_refuse_non_e164_numbers(phone):
    with pytest.raises(ValidationError):
        _user_create(phone)
    with pytest.raises(ValidationError):
        UserUpdate(phone=phone)


@pytest.mark.parametrize("phone", LEADING_ZERO)
def test_address_schemas_refuse_a_leading_zero_country_code(phone):
    for build in (_address_create, lambda p: AddressUpdate(delivery_phone=p)):
        with pytest.raises(ValidationError, match="Invalid phone number format"):
            build(phone)


@pytest.mark.parametrize("phone", TOO_LONG)
def test_address_schemas_refuse_more_than_15_digits(phone):
    for build in (_address_create, lambda p: AddressUpdate(delivery_phone=p)):
        with pytest.raises(ValidationError, match="Invalid phone number length"):
            build(phone)


@pytest.mark.parametrize(
    "raw,expected",
    [("+49 30 1234567", "+49301234567"), ("+1 (555) 123-4567", "+15551234567"), ("+44-20-7183-8750", "+442071838750")],
)
def test_address_phone_normalisation_still_strips_separators(raw, expected):
    assert _address_create(raw).delivery_phone == expected
    assert AddressUpdate(delivery_phone=raw).delivery_phone == expected


@pytest.mark.parametrize("phone", [None, ""])
def test_empty_phone_is_still_allowed(phone):
    assert AddressUpdate(delivery_phone=phone).delivery_phone == phone
    assert _address_create(phone).delivery_phone == phone


def test_responses_still_read_a_number_stored_before_the_rule():
    user = SimpleNamespace(
        id=1, email="old@example.com", full_name="Old User", phone="+0000000",
        role="client", is_active=True, is_email_verified=True,
        created_at="2024-01-01T00:00:00Z",
    )
    assert UserResponse.model_validate(user).phone == "+0000000"
    address = SimpleNamespace(
        id=1, street="1 Main Street", city="Berlin", state="Berlin", postal_code="10115",
        country="DE", address_type="home", is_default=False, delivery_instructions=None,
        delivery_phone="+0123456789", created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z",
    )
    assert AddressResponse.model_validate(address, from_attributes=True).delivery_phone == "+0123456789"
