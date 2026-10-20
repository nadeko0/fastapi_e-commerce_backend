"""get_current_user / get_current_active_user / get_current_admin_user
(app/core/security.py) on AsyncSession and the async Redis client.

Covers what the dependencies decide (who gets 401 / 400 / 403), that they
no longer touch the sync Session or the sync RedisService, and the contract
of the object they return: a detached snapshot of the user row.
"""

import asyncio
import time
import uuid

import fakeredis
import jwt
import pytest
from freezegun import freeze_time
from sqlalchemy import event, inspect
from sqlalchemy.orm.exc import DetachedInstanceError

import app.core.database as database_module
import app.core.security as security
import app.services.redis_async as redis_async_module
from app.core.security import (
    blacklist_token,
    create_access_token,
    get_current_user,
    get_password_hash,
    invalidate_tokens_issued_before_now,
)
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.product import Product
from app.models.user import User

USERS_PREFIX = "/api/v1/users"
ADMIN_PREFIX = "/api/v1/admin"
CART_PREFIX = "/api/v1/cart"
ORDERS_PREFIX = "/api/v1/orders"

PASSWORD = "Str0ngPass1"


def _registration_payload(email="auth.user@example.com"):
    return {
        "email": email,
        "password": PASSWORD,
        "full_name": "Auth User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _login(client, email, password=PASSWORD):
    response = client.post(
        f"{USERS_PREFIX}/login", data={"username": email, "password": password}
    )
    return response.json()["data"]["access_token"]


def _register_and_login(client, email="auth.user@example.com"):
    client.post(f"{USERS_PREFIX}/register", json=_registration_payload(email))
    return _login(client, email)


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _make_admin(db_session, email="admin@example.com"):
    admin = User(
        email=email,
        hashed_password=get_password_hash(PASSWORD),
        full_name="Admin User",
        role=UserRole.ADMIN,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def _user_id(db_session, email="auth.user@example.com"):
    return db_session.query(User.id).filter(User.email == email).scalar()


# --- Who is let in ----------------------------------------------------------


def test_valid_token_authenticates(client):
    token = _register_and_login(client)

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["email"] == "auth.user@example.com"


def test_token_blacklisted_through_the_sync_service_is_rejected(client):
    # Logout blacklists through the sync RedisService; the auth dependency
    # reads through the async client. Both must be looking at one keyspace.
    token = _register_and_login(client)
    assert client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token)).status_code == 200

    blacklist_token(token, expires_in=3600)

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))
    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_blacklisted_through_the_async_client_is_seen_by_the_sync_service(client):
    # The other direction: one keyspace, whichever client writes.
    token = _register_and_login(client)
    jti = jwt.decode(token, options={"verify_signature": False})["jti"]

    async def _blacklist():
        await redis_async_module.get_async_redis().set(f"blacklist:jti:{jti}", "1", ex=3600)

    asyncio.run(_blacklist())

    assert security.redis_service.is_blacklisted(f"jti:{jti}") is True
    assert client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token)).status_code == 401


def _sign(claims, key=None, algorithm=None):
    return jwt.encode(
        claims,
        security.settings.JWT_SECRET_KEY if key is None else key,
        algorithm=algorithm or security.settings.JWT_ALGORITHM,
    )


@pytest.mark.parametrize(
    "forge",
    [
        lambda claims: _sign({**claims, "exp": int(time.time()) - 5}),
        lambda claims: _sign(claims, key="somebody-else's-key-of-32-bytes!"),
        lambda claims: jwt.encode(claims, None, algorithm="none"),
        lambda claims: _sign(claims, algorithm="HS512"),
        lambda claims: "not.a.token",
    ],
    ids=["expired", "wrong-key", "alg-none", "other-alg", "garbage"],
)
def test_a_token_the_jwt_library_refuses_is_a_401(client, db_session, forge):
    # A real user, so that the token is the only thing wrong. Each case is a
    # different PyJWTError subclass, and every one must end as a 401.
    client.post(f"{USERS_PREFIX}/register", json=_registration_payload())
    claims = {"sub": str(_user_id(db_session)), "type": "access", "exp": int(time.time()) + 600}

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(forge(claims)))

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_logout_through_the_api_invalidates_the_token(client):
    token = _register_and_login(client)

    logout = client.post(f"{USERS_PREFIX}/logout", headers=_auth_headers(token))
    assert logout.status_code == 200

    assert client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token)).status_code == 401


def test_token_issued_before_a_password_change_is_rejected(client, db_session):
    client.post(f"{USERS_PREFIX}/register", json=_registration_payload())
    user_id = _user_id(db_session)

    with freeze_time("2026-01-01 12:00:00") as frozen:
        old_token = create_access_token(user_id)
        assert client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(old_token)).status_code == 200

        frozen.tick(delta=5)
        # What a password reset records (app/api/v1/users.py), written
        # through the sync service.
        assert invalidate_tokens_issued_before_now(user_id)

        rejected = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(old_token))

        frozen.tick(delta=5)
        new_token = create_access_token(user_id)
        accepted = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(new_token))

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_password_change_of_one_user_does_not_invalidate_another_users_token(client, db_session):
    client.post(f"{USERS_PREFIX}/register", json=_registration_payload())
    client.post(f"{USERS_PREFIX}/register", json=_registration_payload("other@example.com"))
    user_id = _user_id(db_session)
    other_id = _user_id(db_session, "other@example.com")

    with freeze_time("2026-01-01 12:00:00") as frozen:
        token = create_access_token(user_id)
        frozen.tick(delta=5)
        invalidate_tokens_issued_before_now(other_id)

        response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))

    assert response.status_code == 200


def test_token_is_rejected_while_redis_is_unreachable(client, monkeypatch):
    # The blacklist cannot be consulted, so the token cannot be trusted:
    # same fail-closed behaviour as RedisService.is_blacklisted.
    token = _register_and_login(client)
    down_server = fakeredis.FakeServer()
    down_server.connected = False
    monkeypatch.setattr(
        redis_async_module,
        "_create_client",
        lambda: fakeredis.FakeAsyncRedis(server=down_server, decode_responses=True),
    )
    monkeypatch.setattr(redis_async_module, "_clients", {})

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))

    assert response.status_code == 401


def test_token_for_a_user_that_no_longer_exists_is_rejected(client, db_session):
    token = _register_and_login(client)
    db_session.query(User).filter(User.email == "auth.user@example.com").delete()
    db_session.commit()

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))

    assert response.status_code == 401


def test_inactive_user_is_rejected_with_400(client, db_session):
    token = _register_and_login(client)
    db_session.query(User).filter(User.email == "auth.user@example.com").update(
        {User.is_active: False}
    )
    db_session.commit()

    response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))

    assert response.status_code == 400
    assert response.json()["detail"] == "Inactive user"


def test_non_admin_is_rejected_on_an_admin_route_with_403(client):
    token = _register_and_login(client)

    response = client.get(f"{ADMIN_PREFIX}/stats", headers=_auth_headers(token))

    assert response.status_code == 403
    assert response.json()["detail"] == "The user doesn't have enough privileges"


def test_admin_is_let_through_on_an_admin_route(client, db_session):
    _make_admin(db_session)
    token = _login(client, "admin@example.com")

    response = client.get(f"{ADMIN_PREFIX}/stats", headers=_auth_headers(token))

    assert response.status_code == 200


def test_inactive_admin_is_rejected_before_the_role_check(client, db_session):
    admin = _make_admin(db_session)
    token = _login(client, "admin@example.com")
    admin.is_active = False
    db_session.commit()

    response = client.get(f"{ADMIN_PREFIX}/stats", headers=_auth_headers(token))

    assert response.status_code == 400


# --- Nothing synchronous left on the request path ---------------------------


def test_authentication_uses_neither_the_sync_session_nor_the_sync_redis_service(
    client, db_session, monkeypatch
):
    # GET /users/me does nothing but authenticate and serialize the user,
    # so anything executed on the sync engine, or asked of the sync
    # RedisService, during it would be the auth dependency's doing.
    token = _register_and_login(client)
    sync_statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        sync_statements.append(statement)

    def _forbidden(*args, **kwargs):
        raise AssertionError("sync RedisService used while authenticating")

    monkeypatch.setattr(security.redis_service, "is_blacklisted", _forbidden)
    monkeypatch.setattr(security.redis_service, "get", _forbidden)
    event.listen(db_session.get_bind(), "before_cursor_execute", _record)
    try:
        response = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", _record)

    assert response.status_code == 200
    assert sync_statements == []


# --- The optional caller of the cart routes ----------------------------------
#
# get_optional_active_user: no Authorization header is a guest (None); with
# one, the request goes through the same three checks as everywhere else.


def test_a_request_without_authorization_authenticates_nobody(client, monkeypatch):
    # A guest's cart request must not cost a user lookup or a Redis round
    # trip for a token that is not there.
    _register_and_login(client)
    async_statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        async_statements.append(statement)

    async def _forbidden(*args, **kwargs):
        raise AssertionError("token checked on a request that carries none")

    monkeypatch.setattr(security, "is_token_blacklisted", _forbidden)
    monkeypatch.setattr(security, "get_password_changed_at", _forbidden)
    async_engine = database_module.AsyncSessionLocal.kw["bind"].sync_engine
    event.listen(async_engine, "before_cursor_execute", _record)
    try:
        response = client.get(CART_PREFIX)
    finally:
        event.remove(async_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert async_statements == []


def test_the_optional_caller_is_held_to_every_check_of_the_required_one(client, db_session):
    token = _register_and_login(client)
    user_id = _user_id(db_session)
    assert client.get(CART_PREFIX, headers=_auth_headers(token)).status_code == 200

    # Issued before a password change (frozen time, as in
    # test_token_issued_before_a_password_change_is_rejected above).
    with freeze_time("2026-01-01 12:00:00") as frozen:
        older = create_access_token(user_id)
        assert client.get(CART_PREFIX, headers=_auth_headers(older)).status_code == 200
        frozen.tick(delta=5)
        invalidate_tokens_issued_before_now(user_id)
        assert client.get(CART_PREFIX, headers=_auth_headers(older)).status_code == 401
    security.redis_service.delete(f"pwd_changed:{user_id}")

    # Blacklisted (logged out). A token of its own: one issued for the same
    # user in the same second as `token` would be that very token.
    with freeze_time("2026-01-01 12:00:00"):
        logged_out = create_access_token(user_id)
        blacklist_token(logged_out, 600)
        assert client.get(CART_PREFIX, headers=_auth_headers(logged_out)).status_code == 401

    # A user that no longer exists.
    assert client.get(
        CART_PREFIX, headers=_auth_headers(create_access_token(user_id + 1000))
    ).status_code == 401

    # Inactive: 400, as from get_current_active_user.
    assert client.get(CART_PREFIX, headers=_auth_headers(token)).status_code == 200
    db_session.query(User).filter(User.id == user_id).update({User.is_active: False})
    db_session.commit()
    inactive = client.get(CART_PREFIX, headers=_auth_headers(token))
    assert inactive.status_code == 400
    assert inactive.json()["detail"] == "Inactive user"


def test_cart_routes_still_declare_the_bearer_scheme_in_openapi(client):
    # Otherwise the docs' "Authorize" button would stop sending the token to
    # the cart routes, and every call made from there would be a guest's.
    schema = client.get("/api/v1/openapi.json").json()

    for path, method in [
        ("/api/v1/cart", "get"), ("/api/v1/cart", "delete"), ("/api/v1/cart/items", "post"),
        ("/api/v1/cart/items/{product_id}", "put"), ("/api/v1/cart/items/{product_id}", "delete"),
        ("/api/v1/cart/validate", "post"),
    ]:
        assert schema["paths"][path][method]["security"] == [{"OAuth2PasswordBearer": []}]


# --- The returned user is a detached snapshot -------------------------------


def _resolve_current_user(token):
    """Run get_current_user the way FastAPI does: on a session from the real
    get_async_db dependency, inspected before that dependency is torn down."""

    async def scenario():
        dependency = database_module.get_async_db()
        db = await anext(dependency)
        try:
            user = await get_current_user(db=db, token=token)
            return user, db.in_transaction(), user in db
        finally:
            await dependency.aclose()

    return asyncio.run(scenario())


def test_current_user_is_detached_with_every_column_loaded(client, db_session):
    token = _register_and_login(client)

    user, session_in_transaction, user_in_session = _resolve_current_user(token)

    assert inspect(user).detached
    assert user_in_session is False
    # No column is left to be loaded later - there is no session to load it.
    assert inspect(user).unloaded == {"addresses", "orders"}
    assert user.email == "auth.user@example.com"
    assert user.role == UserRole.CLIENT
    assert user.is_active is True
    assert user.consent_history


def test_auth_releases_its_connection_before_the_route_runs(client):
    # The session lives until the request's dependencies are torn down. If
    # the read transaction were left open it would pin a pooled connection
    # for the whole request, on top of the route's own.
    token = _register_and_login(client)

    _, session_in_transaction, _ = _resolve_current_user(token)

    assert session_in_transaction is False


def test_reading_a_relationship_off_current_user_fails_loudly(client):
    # Not a supported use - routes query through their own session - but it
    # must be an error, never a silent empty list.
    token = _register_and_login(client)

    user, _, _ = _resolve_current_user(token)

    with pytest.raises(DetachedInstanceError):
        user.addresses
    with pytest.raises(DetachedInstanceError):
        user.orders


def test_changing_current_user_persists_nothing(client, db_session):
    token = _register_and_login(client)

    async def scenario():
        dependency = database_module.get_async_db()
        db = await anext(dependency)
        user = await get_current_user(db=db, token=token)
        user.full_name = "Changed In Memory"
        user.is_active = False
        # Finish the dependency the way FastAPI does after a successful
        # request: get_async_db commits its session.
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)

    asyncio.run(scenario())

    stored = db_session.query(User).filter(User.email == "auth.user@example.com").one()
    assert stored.full_name == "Auth User"
    assert stored.is_active is True


# --- Regression: POST /orders read current_user.addresses --------------------


def _checkout_ready_buyer(client, db_session, email):
    token = _register_and_login(client, email)
    client.put(
        f"{USERS_PREFIX}/me",
        json={"full_name": "Buyer Person", "phone": "+12025550123"},
        headers=_auth_headers(token),
    )
    db_session.query(User).filter(User.email == email).update({User.is_email_verified: True})
    db_session.commit()
    address = client.post(
        f"{USERS_PREFIX}/addresses",
        json={
            "street": "123 Main Street",
            "city": "Springfield",
            "state": "Illinois",
            "postal_code": "62701",
            "country": "US",
        },
        headers=_auth_headers(token),
    )
    return token, address.json()["data"]["id"]


def _product_in_cart(client, db_session, token):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price="19.99",
        stock_quantity=10,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    added = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    assert added.status_code == 200


def _place_order(client, token, address_id):
    return client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers={"Idempotency-Key": uuid.uuid4().hex, **_auth_headers(token)},
    )


def test_create_order_finds_the_users_own_shipping_address(client, db_session):
    # create_order used to look the address up in current_user.addresses, a
    # lazy relationship that a detached user cannot load.
    token, address_id = _checkout_ready_buyer(client, db_session, "buyer@example.com")
    _product_in_cart(client, db_session, token)

    response = _place_order(client, token, address_id)

    assert response.status_code == 200
    assert response.json()["data"]["shipping_address"]["id"] == address_id


def test_create_order_rejects_another_users_shipping_address(client, db_session):
    _, foreign_address_id = _checkout_ready_buyer(client, db_session, "owner@example.com")
    token, _ = _checkout_ready_buyer(client, db_session, "buyer@example.com")
    _product_in_cart(client, db_session, token)

    response = _place_order(client, token, foreign_address_id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Shipping address not found"


def test_create_order_still_accepts_a_deactivated_address_of_the_user(client, db_session):
    # Pins that moving the lookup into a query did not change which rows
    # qualify: current_user.addresses held every address of the user,
    # deactivated ("deleted") ones included.
    token, address_id = _checkout_ready_buyer(client, db_session, "buyer@example.com")
    db_session.query(Address).filter(Address.id == address_id).update({Address.is_active: False})
    db_session.commit()
    _product_in_cart(client, db_session, token)

    response = _place_order(client, token, address_id)

    assert response.status_code == 200
