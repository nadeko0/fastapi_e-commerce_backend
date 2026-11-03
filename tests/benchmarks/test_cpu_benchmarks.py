"""Pure CPU benchmarks for hot path operations executed per request.

Measures etag generation and parsing, JWT token encode and decode,
Pydantic model serialization, catalog filter parameter validation,
and cart money and minor unit calculations.
"""
from decimal import Decimal

from app.api.v1.orders import _decimal_to_cents
from app.core.clock import utc_now
from app.core.etag import etag_for, if_none_match_hits
from app.core.security import (
    ACCESS_TOKEN_REQUIRED_CLAIMS,
    create_access_token,
    decode_token,
)
from app.schemas.common import Cart, CartItem, CartResponse
from app.schemas.product import (
    ProductCharacteristic,
    ProductFilter,
    ProductListItem,
    ProductListResponse,
    ProductResponse,
)


def _make_sample_product_response(product_id: int) -> ProductResponse:
    now = utc_now()
    return ProductResponse(
        id=product_id,
        name=f"Sample Product {product_id}",
        description=f"A realistic sample product description for product {product_id} with detailed specifications.",
        price=Decimal("49.99"),
        stock_quantity=150,
        category_id=1,
        category_name="Electronics",
        currency="USD",
        images=["https://example.com/images/sample-product.png"],
        characteristics={
            "color": ProductCharacteristic(name="color", value="Space Gray"),
            "storage": ProductCharacteristic(name="storage", value="256GB"),
            "weight": ProductCharacteristic(name="weight", value="180", unit="g"),
        },
        created_at=now,
        updated_at=now,
    )


def test_bench_etag_for(benchmark):
    model = _make_sample_product_response(1)
    etag = benchmark(etag_for, model)
    assert etag.startswith('"') and etag.endswith('"')
    assert len(etag) == 34


def test_bench_if_none_match_parser(benchmark):
    header = 'W/"stale-tag-12345", "0123456789abcdef0123456789abcdef", "another-tag-67890"'
    current_etag = '"0123456789abcdef0123456789abcdef"'
    hit = benchmark(if_none_match_hits, header, current_etag)
    assert hit is True


def test_bench_jwt_encode(benchmark):
    token = benchmark(create_access_token, subject=1001)
    assert isinstance(token, str)
    assert len(token) > 20


def test_bench_jwt_decode(benchmark):
    token = create_access_token(subject=1001)
    payload = benchmark(decode_token, token, options={"require": ACCESS_TOKEN_REQUIRED_CLAIMS})
    assert payload["sub"] == "1001"
    assert payload["type"] == "access"


def test_bench_pydantic_product_list_serialization(benchmark):
    items = [
        ProductListItem(**_make_sample_product_response(i).model_dump(), has_variants=False)
        for i in range(1, 21)
    ]
    product_list = ProductListResponse(
        items=items,
        total=100,
        page=1,
        size=20,
        has_more=True,
    )
    serialized = benchmark(product_list.model_dump_json)
    assert isinstance(serialized, str)
    assert len(serialized) > 500


def test_bench_filter_params_parsing(benchmark):
    def parse_filter():
        return ProductFilter(
            category_id=1,
            min_price=Decimal("10.00"),
            max_price=Decimal("99.99"),
            in_stock=True,
            sort_by="price_desc",
            page=1,
            size=20,
        )

    parsed = benchmark(parse_filter)
    assert parsed.category_id == 1
    assert parsed.sort_by == "price_desc"


class _CatalogItem:
    def __init__(self, name: str, price: Decimal, is_active: bool = True, images: list[str] | None = None):
        self.name = name
        self.price = price
        self.is_active = is_active
        self.images = images or []


class _CatalogVariant:
    def __init__(self, product_id: int, price_override: Decimal | None = None, is_active: bool = True):
        self.product_id = product_id
        self.price_override = price_override
        self.is_active = is_active


def test_bench_order_money_calculation(benchmark):
    now = utc_now()
    cart = Cart(
        user_id=101,
        items={
            "1": CartItem(product_id=1, quantity=3, added_at=now),
            "2:201": CartItem(product_id=2, variant_id=201, quantity=2, added_at=now),
            "3": CartItem(product_id=3, quantity=1, added_at=now),
            "4": CartItem(product_id=4, quantity=5, added_at=now),
            "5:501": CartItem(product_id=5, variant_id=501, quantity=1, added_at=now),
        },
        expires_at=now,
    )
    products = {
        1: _CatalogItem("Widget A", Decimal("19.99"), images=["https://example.com/a.png"]),
        2: _CatalogItem("Widget B", Decimal("49.99"), images=["https://example.com/b.png"]),
        3: _CatalogItem("Widget C", Decimal("9.95"), images=["https://example.com/c.png"]),
        4: _CatalogItem("Widget D", Decimal("12.50"), images=["https://example.com/d.png"]),
        5: _CatalogItem("Widget E", Decimal("99.00"), images=["https://example.com/e.png"]),
    }
    variants = {
        201: _CatalogVariant(product_id=2, price_override=Decimal("45.00")),
        501: _CatalogVariant(product_id=5, price_override=Decimal("89.00")),
    }

    def calculate_cart_totals():
        response = CartResponse.from_cart(cart, products, variants)
        cents = _decimal_to_cents(response.total_amount)
        return response.total_amount, cents

    total_amount, cents = benchmark(calculate_cart_totals)
    assert total_amount == Decimal("311.42")
    assert cents == 31142
