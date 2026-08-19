"""Seed a demo dataset for local development / evaluation.

Populates a small, generic-marketplace catalog (categories are illustrative
placeholders, not "the" domain this backend is for - swap them for whatever
you're actually selling): a couple of top-level categories with
subcategories, a handful of realistic-looking products spread across them
(a few of which have variants, via app/models/product.py's ProductVariant),
one admin user and one regular user.

Run after `alembic upgrade head`, against a real (ideally disposable/local)
Postgres database:

    uv run python scripts/seed_demo_data.py

Idempotent-safe-enough for repeated demo use: every insert is guarded by a
"does this already exist" check (by category name, product name, or user
email), so re-running against a DB that already has this seed data will
skip what's already there rather than duplicate it or error. It is not a
full upsert framework - if you hand-edit a seeded row and re-run, your edit
is left alone (the row is skipped, not reconciled).

The admin and regular-user passwords are generated fresh each run and
printed to stdout - they are never hardcoded, and this script does not
persist them anywhere else. Save them before closing the terminal; you'll
need to log in again (or reset) if you lose them.
"""
import os
import secrets
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.product import Product, ProductVariant  # noqa: E402
from app.models.user import User  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
TEST_USER_EMAIL = "demo.user@example.com"

# --- Category tree (illustrative - a generic "general store" catalog) -----
# Each top-level entry: (name, description, [ (subcategory name, description), ... ])
CATEGORY_TREE = [
    (
        "Home & Garden",
        "Everyday items for the home and outdoor living spaces.",
        [
            ("Kitchen", "Cookware, drinkware, and kitchen essentials."),
            ("Furniture", "Desks, chairs, and other furniture."),
        ],
    ),
    (
        "Office Supplies",
        "Tools and accessories for a home or professional office.",
        [
            ("Stationery", "Pens, notebooks, and paper goods."),
            ("Electronics Accessories", "Peripherals and small electronics."),
        ],
    ),
    (
        "Outdoor & Recreation",
        "Gear for camping, sports, and general outdoor activity.",
        [
            ("Camping", "Tents, gear, and camp essentials."),
            ("Sports", "Fitness and sporting equipment."),
        ],
    ),
]

# --- Products ---------------------------------------------------------
# (name, description, price, stock_quantity, subcategory_name, images, variants)
# variants is a list of (sku, attributes, price_override, stock_quantity) or None.
PRODUCTS = [
    (
        "Ceramic Coffee Mug Set (4-Pack)",
        "Set of four 12oz stoneware mugs, dishwasher and microwave safe.",
        Decimal("24.99"), 60, "Kitchen",
        ["https://example.com/images/mug-set.jpg"], None,
    ),
    (
        "Cast Iron Skillet, 10-inch",
        "Pre-seasoned cast iron skillet, suitable for stovetop and oven use.",
        Decimal("39.99"), 45, "Kitchen",
        ["https://example.com/images/skillet.jpg"], None,
    ),
    (
        "Adjustable Standing Desk",
        "Electric height-adjustable desk, 55x28 inch top, memory presets.",
        Decimal("349.00"), 15, "Furniture",
        ["https://example.com/images/standing-desk.jpg"], None,
    ),
    (
        "Ergonomic Office Chair",
        "Mesh-back office chair with adjustable lumbar support and armrests.",
        Decimal("189.50"), 25, "Furniture",
        ["https://example.com/images/office-chair.jpg"],
        [
            ("CHAIR-ERG-BLK", {"color": "black"}, None, 10),
            ("CHAIR-ERG-GRY", {"color": "gray"}, None, 8),
            ("CHAIR-ERG-BLU", {"color": "blue"}, Decimal("199.50"), 7),
        ],
    ),
    (
        "Wireless Optical Mouse",
        "2.4GHz wireless mouse with adjustable DPI and USB-A receiver.",
        Decimal("19.99"), 100, "Electronics Accessories",
        ["https://example.com/images/wireless-mouse.jpg"], None,
    ),
    (
        "Mechanical Keyboard, Compact",
        "80% layout mechanical keyboard with hot-swappable switches.",
        Decimal("89.99"), 40, "Electronics Accessories",
        ["https://example.com/images/mech-keyboard.jpg"],
        [
            ("KB-COMPACT-LIN", {"switch_type": "linear"}, None, 20),
            ("KB-COMPACT-TAC", {"switch_type": "tactile"}, None, 15),
            ("KB-COMPACT-CLK", {"switch_type": "clicky"}, Decimal("94.99"), 5),
        ],
    ),
    (
        "Recycled Paper Notebook (3-Pack)",
        "A5 lined notebooks, 120 pages each, 100% recycled paper.",
        Decimal("12.99"), 150, "Stationery",
        ["https://example.com/images/notebooks.jpg"], None,
    ),
    (
        "Fountain Pen, Fine Nib",
        "Refillable fountain pen with a stainless steel fine nib.",
        Decimal("45.00"), 30, "Stationery",
        ["https://example.com/images/fountain-pen.jpg"], None,
    ),
    (
        "4-Person Dome Tent",
        "Weatherproof dome tent with rainfly, sets up in under 10 minutes.",
        Decimal("159.99"), 20, "Camping",
        ["https://example.com/images/dome-tent.jpg"], None,
    ),
    (
        "Insulated Steel Water Bottle",
        "24oz double-wall insulated bottle, keeps drinks cold for 24 hours.",
        Decimal("19.99"), 80, "Camping",
        ["https://example.com/images/water-bottle.jpg"], None,
    ),
    (
        "Non-Slip Yoga Mat",
        "6mm thick exercise mat with carrying strap.",
        Decimal("34.99"), 55, "Sports",
        ["https://example.com/images/yoga-mat.jpg"], None,
    ),
    (
        "Adjustable Dumbbell Set (Pair)",
        "Pair of adjustable dumbbells, 5-25 lbs each in 5 lb increments.",
        Decimal("129.99"), 18, "Sports",
        ["https://example.com/images/dumbbells.jpg"], None,
    ),
]


def get_or_create_category(db, name, description, parent=None):
    existing = db.query(Category).filter(Category.name == name).first()
    if existing:
        print(f"  [skip] category already exists: {name}")
        return existing

    category = Category(
        name=name,
        description=description,
        parent_id=parent.id if parent else None,
        level=(parent.level + 1) if parent else 0,
        path=(parent.path + [parent.id]) if parent else [],
    )
    db.add(category)
    db.commit()
    db.refresh(category)
    print(f"  [created] category: {name} (level={category.level})")
    return category


def get_or_create_product(db, name, description, price, stock_quantity, category_id, images):
    existing = db.query(Product).filter(Product.name == name).first()
    if existing:
        print(f"  [skip] product already exists: {name}")
        return existing

    product = Product(
        name=name,
        description=description,
        price=price,
        stock_quantity=stock_quantity,
        category_id=category_id,
        images=images,
        characteristics={},
        is_active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    print(f"  [created] product: {name} (${price})")
    return product


def get_or_create_variant(db, product_id, sku, attributes, price_override, stock_quantity):
    existing = db.query(ProductVariant).filter(ProductVariant.sku == sku).first()
    if existing:
        print(f"    [skip] variant already exists: {sku}")
        return existing

    variant = ProductVariant(
        product_id=product_id,
        sku=sku,
        attributes=attributes,
        price_override=price_override,
        stock_quantity=stock_quantity,
        is_active=True,
    )
    db.add(variant)
    db.commit()
    db.refresh(variant)
    print(f"    [created] variant: {sku} {attributes}")
    return variant


def get_or_create_user(db, email, full_name, role, password):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        print(f"  [skip] user already exists: {email}")
        return existing, False

    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        full_name=full_name,
        role=role,
        is_active=True,
        is_email_verified=True,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        data_export_requests=[],
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    print(f"  [created] user: {email} ({role.value})")
    return user, True


def main():
    db = SessionLocal()
    admin_password = secrets.token_urlsafe(12)
    user_password = secrets.token_urlsafe(12)

    try:
        print("Seeding categories...")
        subcategories_by_name = {}
        for top_name, top_desc, subs in CATEGORY_TREE:
            top = get_or_create_category(db, top_name, top_desc)
            for sub_name, sub_desc in subs:
                sub = get_or_create_category(db, sub_name, sub_desc, parent=top)
                subcategories_by_name[sub_name] = sub

        print("\nSeeding products...")
        for name, description, price, stock, subcat_name, images, variants in PRODUCTS:
            category = subcategories_by_name[subcat_name]
            product = get_or_create_product(
                db, name, description, price, stock, category.id, images
            )
            if variants:
                for sku, attributes, price_override, variant_stock in variants:
                    get_or_create_variant(
                        db, product.id, sku, attributes, price_override, variant_stock
                    )

        print("\nSeeding users...")
        admin, admin_created = get_or_create_user(
            db, ADMIN_EMAIL, "Demo Admin", UserRole.ADMIN, admin_password
        )
        user, user_created = get_or_create_user(
            db, TEST_USER_EMAIL, "Demo User", UserRole.CLIENT, user_password
        )

        print("\nDone.")
        print("=" * 60)
        if admin_created:
            print(f"Admin login   : {ADMIN_EMAIL} / {admin_password}")
        else:
            print(f"Admin login   : {ADMIN_EMAIL} (already existed - password unchanged)")
        if user_created:
            print(f"Test user login: {TEST_USER_EMAIL} / {user_password}")
        else:
            print(f"Test user login: {TEST_USER_EMAIL} (already existed - password unchanged)")
        print("=" * 60)
        print("Save these credentials now - they are not stored anywhere.")
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
