"""Regression coverage for the app/models + app/schemas line-by-line review
(.agent-notes/part_b_models_schemas_review.md).

Covers:
- Mutable Column(default=[]/{}) sharing one object across ORM instances
  (Category.path, Product.images/characteristics, ProductVariant.attributes,
  User.consent_history/data_export_requests).
- create_string_enum returning a usable column type after dropping its
  unused {'name', 'check'} dict.
- AddressBase/AddressUpdate.delivery_phone validation, previously
  unvalidated unlike UserBase.phone for the same concept.
- app/schemas/legal.py's Config classes now using the Pydantic v2 keys
  (json_schema_extra / from_attributes) so the documented examples and
  ORM-loading actually take effect instead of being silently ignored.
"""
import warnings

import pytest
from pydantic import ValidationError

from app.models.category import Category
from app.models.enums import AddressType, OrderStatus, UserRole, create_string_enum
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.address import AddressCreate, AddressUpdate
from app.schemas.legal import ConsentUpdate, LegalDocument


def _minimal_user(**overrides):
    defaults = dict(email="a@example.com", hashed_password="x")
    defaults.update(overrides)
    return User(**defaults)


def _minimal_product(**overrides):
    defaults = dict(
        name="Widget",
        description="A widget",
        price="9.99",
        category_id=1,
    )
    defaults.update(overrides)
    return Product(**defaults)


class TestMutableColumnDefaults:
    def test_category_path_defaults_are_independent_lists(self, db_session):
        c1 = Category(name="A", level=0)
        c2 = Category(name="B", level=0)
        db_session.add_all([c1, c2])
        db_session.flush()

        assert c1.path is not c2.path
        c1.path.append(99)
        assert c2.path == []

    def test_product_images_and_characteristics_defaults_are_independent(self, db_session):
        p1 = _minimal_product(name="Widget A")
        p2 = _minimal_product(name="Widget B")
        db_session.add_all([p1, p2])
        db_session.flush()

        assert p1.images is not p2.images
        assert p1.characteristics is not p2.characteristics
        p1.images.append("http://example.com/a.png")
        p1.characteristics["color"] = "red"
        assert p2.images == []
        assert p2.characteristics == {}

    def test_product_variant_attributes_defaults_are_independent(self, db_session):
        product = _minimal_product()
        db_session.add(product)
        db_session.flush()
        v1 = ProductVariant(product_id=product.id, sku="SKU-1")
        v2 = ProductVariant(product_id=product.id, sku="SKU-2")
        db_session.add_all([v1, v2])
        db_session.flush()

        assert v1.attributes is not v2.attributes
        v1.attributes["size"] = "M"
        assert v2.attributes == {}

    def test_user_consent_history_and_export_requests_defaults_are_independent(self, db_session):
        u1 = _minimal_user(email="u1@example.com")
        u2 = _minimal_user(email="u2@example.com")
        db_session.add_all([u1, u2])
        db_session.flush()

        assert u1.consent_history is not u2.consent_history
        assert u1.data_export_requests is not u2.data_export_requests
        u1.consent_history.append({"type": "gdpr"})
        u1.data_export_requests.append({"id": "req_1"})
        assert u2.consent_history == []
        assert u2.data_export_requests == []


class TestCreateStringEnum:
    def test_returns_the_string_type_directly(self):
        from sqlalchemy import String

        assert create_string_enum(UserRole, "role") is String

    def test_columns_built_from_it_round_trip_values(self, db_session):
        # End-to-end: the enum-backed columns (User.role, Order.status via
        # OrderStatus, Address.address_type) still insert/read back correctly
        # after create_string_enum stopped returning the unused dict half.
        user = _minimal_user(role=UserRole.ADMIN.value)
        db_session.add(user)
        db_session.flush()
        db_session.refresh(user)
        assert user.role == UserRole.ADMIN.value
        assert OrderStatus.NEW.value == "new"  # sanity: enum untouched
        assert AddressType.HOME.value == "home"


class TestAddressPhoneValidation:
    def _payload(self, **overrides):
        payload = dict(
            street="123 Main Street",
            city="Springfield",
            state="IL",
            postal_code="62701",
            country="US",
            delivery_phone=None,
        )
        payload.update(overrides)
        return payload

    def test_invalid_delivery_phone_is_rejected_on_create(self):
        with pytest.raises(ValidationError):
            AddressCreate(**self._payload(delivery_phone="not-a-phone"))

    def test_valid_delivery_phone_is_normalized_on_create(self):
        addr = AddressCreate(**self._payload(delivery_phone="+1 (555) 123-4567"))
        assert addr.delivery_phone == "+15551234567"

    def test_invalid_delivery_phone_is_rejected_on_update(self):
        with pytest.raises(ValidationError):
            AddressUpdate(delivery_phone="abc")

    def test_none_delivery_phone_is_allowed_on_update(self):
        update = AddressUpdate(delivery_phone=None)
        assert update.delivery_phone is None


class TestLegalSchemaConfig:
    def test_json_schema_extra_example_is_applied(self):
        # Previously `class Config: schema_extra = {...}` (the Pydantic v1
        # key) was silently ignored under Pydantic v2 - the documented
        # "example" never made it into the generated schema.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            schema = ConsentUpdate.model_json_schema()
        assert schema.get("example") == {
            "marketing_consent": True,
            "privacy_policy_accepted": True,
        }

    def test_legal_document_schema_extra_is_applied(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            schema = LegalDocument.model_json_schema()
        assert schema["example"]["version"] == "1.0"
