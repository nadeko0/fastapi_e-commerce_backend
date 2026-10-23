"""Nothing a coroutine route does may block the event loop.

Two checks keep the async migration honest.

1. A static guard. For every `async def` route, walk the full dependency
   tree FastAPI resolved (route.dependant) and fail on a synchronous
   resource: either `get_db`, or the synchronous RedisService class. The
   endpoint's own source is scanned too (AST) for the same things used
   directly: `.query(`, RedisService / redis_service, `.apply_async`. Sync
   code reached through `await db.run_sync(...)` or `run_in_threadpool(...)`
   is allowed: it is not written in the endpoint body and runs off the loop.

2. A behavioural check per route group. N concurrent requests run on one
   event loop next to a heartbeat task; synchronous boundaries are poisoned
   with a sleep, so any blocking call that is still on the loop shows as a
   gap in the heartbeat, and async latency is injected into the database and
   Redis, so a request that waits one call after another (instead of the
   loop serving others meanwhile) shows as wall time growing with N.

PENDING_ROUTERS lists the routers that are not migrated yet. The set is kept
honest in both directions: a pending router must still fail the guard, and a
router that passes it must not be listed. A migrating task therefore (1)
removes its line, sees the guard fail, migrates until it passes, and (2)
makes sure its group in ROUTE_GROUPS below has a scenario: the behavioural
test of an enabled group without one fails.
"""

import ast
import asyncio
import inspect
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.deps as deps_module
import app.core.database as database_module
import app.services.redis_async as redis_async
from app.core.clock import utc_now
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.category import Category
from app.models.product import Product
from app.models.user import User, UserRole
from app.services.redis import RedisService

# One router per line, a blank line between: every router task deletes its
# own line, and the diff of two tasks never touches the same hunk.
PENDING_ROUTERS = {
    "users",

    "orders",

}

# --- 1. The static guard ----------------------------------------------------

SYNC_DEPENDENCIES = {
    database_module.get_db: "sync session (app.core.database.get_db)",
    deps_module.get_db: "sync session (app.api.deps.get_db)",
    RedisService: "sync Redis (RedisService)",
}
SYNC_NAMES_IN_BODY = {"RedisService", "redis_service"}


def api_routes(routes):
    """Every APIRoute, including those of routers added with
    include_router(), which current FastAPI exposes through wrapper
    objects holding the original router."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from api_routes(route.original_router.routes)


def dependency_calls(dependant):
    """Every callable in the dependency tree below `dependant`."""
    for sub in dependant.dependencies:
        yield sub.call
        yield from dependency_calls(sub)


def router_of(endpoint) -> str:
    """The router an endpoint belongs to: the last part of its module name
    (app.api.v1.users -> users)."""
    return inspect.unwrap(endpoint).__module__.rsplit(".", 1)[-1]


def body_violations(endpoint) -> List[str]:
    """Synchronous things used directly in the endpoint's source."""
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(endpoint)))
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "query":
                found.add("Session.query() in the body")
            if node.func.attr == "apply_async":
                found.add("Celery apply_async() in the body (use run_in_threadpool)")
        if isinstance(node, ast.Name) and node.id in SYNC_NAMES_IN_BODY:
            found.add(f"{node.id} in the body")
        if isinstance(node, ast.Attribute) and node.attr in SYNC_NAMES_IN_BODY:
            found.add(f"{node.attr} in the body")
    return sorted(found)


def route_violations(route: APIRoute) -> List[str]:
    """What keeps this route from being loop-safe; empty for a sync `def`
    endpoint (FastAPI runs those in a worker thread) and for a clean one."""
    if not inspect.iscoroutinefunction(inspect.unwrap(route.endpoint)):
        return []
    found = {
        SYNC_DEPENDENCIES[call]
        for call in dependency_calls(route.dependant)
        if call in SYNC_DEPENDENCIES
    }
    return sorted(found) + body_violations(route.endpoint)


def violations_by_router(routes) -> Dict[str, Dict[str, List[str]]]:
    """{router: {"METHOD path": [violations]}}, only for what fails."""
    found: Dict[str, Dict[str, List[str]]] = {}
    seen = set()
    for route in api_routes(routes):
        seen.add(router_of(route.endpoint))
        problems = route_violations(route)
        if problems:
            label = f"{','.join(sorted(route.methods))} {route.path}"
            found.setdefault(router_of(route.endpoint), {})[label] = problems
    for router in seen:
        found.setdefault(router, {})
    return {router: found[router] for router in sorted(found)}


def _failing_routers() -> set:
    return {router for router, problems in violations_by_router(app.routes).items() if problems}


def test_pending_routers_are_exactly_the_routers_that_still_fail_the_guard():
    failing = _failing_routers()

    migrated_but_listed = PENDING_ROUTERS - failing
    unmigrated_but_not_listed = failing - PENDING_ROUTERS
    assert not migrated_but_listed, (
        f"{sorted(migrated_but_listed)} pass the guard: remove them from PENDING_ROUTERS"
    )
    assert not unmigrated_but_not_listed, (
        f"{sorted(unmigrated_but_not_listed)} block the loop: "
        f"{ {r: violations_by_router(app.routes)[r] for r in unmigrated_but_not_listed} }"
    )


def test_every_router_not_pending_is_clean():
    problems = {
        router: found
        for router, found in violations_by_router(app.routes).items()
        if router not in PENDING_ROUTERS and found
    }

    assert problems == {}


def test_pending_names_are_real_routers():
    known = {router_of(route.endpoint) for route in api_routes(app.routes)}

    assert PENDING_ROUTERS <= known


def test_the_guard_finds_each_kind_of_blocking_use():
    probe = FastAPI()

    @probe.get("/session")
    async def session_dependency(db=Depends(database_module.get_db)):
        return 1

    @probe.get("/deps-session")
    async def deps_session_dependency(db=Depends(deps_module.get_db)):
        return 1

    @probe.get("/redis")
    async def redis_dependency(redis: RedisService = Depends()):
        return 1

    def nested(db=Depends(database_module.get_db)):
        return db

    @probe.get("/nested")
    async def nested_dependency(value=Depends(nested)):
        return 1

    @probe.get("/query")
    async def query_in_body(db=Depends(lambda: None)):
        return db.query(User).first()

    @probe.get("/service")
    async def service_in_body():
        return RedisService().get("k")

    @probe.get("/publish")
    async def publish_in_body():
        return object().apply_async()

    @probe.get("/threaded")
    def sync_endpoint(db=Depends(database_module.get_db)):
        return 1

    @probe.get("/clean")
    async def clean(db: AsyncSession = Depends(database_module.get_async_db)):
        return 1

    found = {
        route.path: route_violations(route) for route in api_routes(probe.routes)
    }

    assert any("app.core.database.get_db" in v for v in found["/session"])
    assert any("app.api.deps.get_db" in v for v in found["/deps-session"])
    assert any("RedisService" in v for v in found["/redis"])
    assert any("app.core.database.get_db" in v for v in found["/nested"])
    assert found["/query"] == ["Session.query() in the body"]
    assert "RedisService in the body" in found["/service"]
    assert any("apply_async" in v for v in found["/publish"])
    assert found["/threaded"] == []
    assert found["/clean"] == []


# --- 2. The behavioural check -------------------------------------------------

# How long a call on a synchronous boundary sleeps: if it runs on the loop,
# the loop is stopped that long.
POISON_SECONDS = 0.5
# Latency injected into every async database and Redis call.
LATENCY_SECONDS = 0.02
HEARTBEAT_SECONDS = 0.005
# The heartbeat may be late by noise, never by a poisoned call. Noise under a
# loaded CPU (other test processes, a shared CI runner) reached ~0.09 s, so the
# poison is long enough that the limit sits far from both: a blocked loop
# shows a gap of at least POISON_SECONDS, noise stays well under half of it.
MAX_HEARTBEAT_GAP = POISON_SECONDS / 2
CONCURRENCY = 8
# N concurrent passes must take well under N sequential ones. Genuinely
# overlapping requests take about one pass; one at a time, N of them.
MAX_WALL_FACTOR = 0.5

Request = Tuple[str, str]  # (method, path)


@dataclass
class RouteGroup:
    """Requests that exercise one router's routes.

    `seed(db_session)` creates what they need and returns a dict handed to
    `requests`, which lists the (method, path[, kwargs]) to send, and
    `headers` (a bearer token) when they need a login."""

    seed: Callable
    requests: Callable
    expected_status: int = 200
    headers: Optional[Callable] = None


@dataclass
class Measurement:
    max_gap: float
    wall: float
    statuses: List[int] = field(default_factory=list)


def make_user_headers(db_session, email="load@example.com", role=UserRole.CLIENT):
    """Log a user in without going through the API: an Authorization header
    for a freshly created, active user."""
    user = User(
        email=email,
        hashed_password=get_password_hash("Str0ngPass"),
        full_name="Load User",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        is_active=True,
        is_email_verified=True,
        role=role,
        created_at=utc_now(),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}, user


def _seed_catalog(db_session):
    parent = Category(name="Root", parent_id=None, level=0, path=[])
    db_session.add(parent)
    db_session.commit()
    child = Category(name="Child", parent_id=parent.id, level=1, path=[parent.id])
    db_session.add(child)
    db_session.commit()
    products = []
    for index in range(5):
        product = Product(
            name=f"Widget {index}",
            description="A fine widget for all your widget needs",
            price="9.99",
            stock_quantity=10,
            images=["https://example.com/widget.png"],
            characteristics={},
            category_id=child.id,
        )
        db_session.add(product)
        products.append(product)
    db_session.commit()
    return {"category_id": child.id, "product_id": products[0].id}


# Reads that never touch the synchronous RedisService (it is a declared but
# unused dependency of list_products): loop-safe already, so they are enabled
# even while the products router is still pending.
CATALOG_READS = RouteGroup(
    seed=_seed_catalog,
    requests=lambda ids: [
        ("GET", "/api/v1/products"),
        ("GET", "/api/v1/products?page=1&size=2&sort_by=price_asc"),
        ("GET", "/api/v1/products/search?query=widget"),
        ("GET", "/api/v1/categories"),
        ("GET", f"/api/v1/categories/{ids['category_id']}"),
    ],
)

# The routes that use the Redis cache: the category tree and a product's
# detail page. Enabled when the products router is migrated.
PRODUCT_CACHE_READS = RouteGroup(
    seed=_seed_catalog,
    requests=lambda ids: [
        ("GET", "/api/v1/categories/tree"),
        ("GET", f"/api/v1/products/{ids['product_id']}"),
    ],
)

# The cart of a logged-in user: read, add, change the quantity, validate. No
# removal: the passes run at once on one account's cart, and a removal in one
# would turn the quantity change of another into a 404.
CART_ACCOUNT = RouteGroup(
    seed=_seed_catalog,
    headers=lambda db_session: make_user_headers(db_session, "cart-load@example.com")[0],
    requests=lambda ids: [
        ("GET", "/api/v1/cart"),
        ("POST", f"/api/v1/cart/items?product_id={ids['product_id']}&quantity=1"),
        ("PUT", f"/api/v1/cart/items/{ids['product_id']}?quantity=2"),
        ("POST", "/api/v1/cart/validate"),
    ],
)

# A guest's cart, through the guest cookie the client keeps: the add opens a
# cart and sets the cookie, then a read, a validation and the emptying of the
# cart (which clears the cookie again). Every request is a 200 whatever order
# the concurrent passes interleave in.
CART_GUEST = RouteGroup(
    seed=_seed_catalog,
    requests=lambda ids: [
        ("GET", "/api/v1/cart"),
        ("POST", f"/api/v1/cart/items?product_id={ids['product_id']}&quantity=1"),
        ("GET", "/api/v1/cart"),
        ("POST", "/api/v1/cart/validate"),
        ("DELETE", "/api/v1/cart"),
    ],
)

# A logged-in user's legal routes: the two reads and a consent update (the
# write runs as many times as the reads; SQLite serializes it, which costs
# wall time but never blocks the loop).
LEGAL = RouteGroup(
    seed=lambda db_session: {},
    headers=lambda db_session: make_user_headers(db_session)[0],
    requests=lambda ids: [
        ("GET", "/api/v1/legal/consent-status"),
        ("GET", "/api/v1/legal/data-retention"),
        ("POST", "/api/v1/legal/consent", {"json": {"marketing_consent": True, "privacy_policy_accepted": True}}),
    ],
)

# An admin's reads (orders, statistics, webhook events, reconciliation
# discrepancies) and two writes (a category, a product). SQLite serializes
# the writes, which costs wall time but never blocks the loop; the full
# write-route run is on PostgreSQL.
ADMIN = RouteGroup(
    seed=_seed_catalog,
    headers=lambda db_session: make_user_headers(db_session, email="admin@example.com", role=UserRole.ADMIN)[0],
    requests=lambda ids: [
        ("GET", "/api/v1/admin/orders"),
        ("GET", "/api/v1/admin/stats?period=7d"),
        ("GET", "/api/v1/admin/webhook-events"),
        ("GET", "/api/v1/admin/reconciliation/discrepancies"),
        ("GET", f"/api/v1/admin/products/{ids['product_id']}/variants"),
        ("POST", "/api/v1/admin/categories", {"json": {"name": "Added"}}),
        (
            "POST",
            "/api/v1/admin/products",
            {
                "json": {
                    "name": "Added widget",
                    "description": "A widget added by the load test",
                    "price": "5.00",
                    "stock_quantity": 3,
                    "images": ["https://example.com/widget.png"],
                    "characteristics": {},
                    "category_id": ids["category_id"],
                }
            },
        ),
    ],
)

# group name -> (router that must be migrated for it to run, scenario).
# A router task fills in the scenario of its router (None = not written
# yet, which fails the test as soon as the router leaves PENDING_ROUTERS).
ROUTE_GROUPS: Dict[str, Tuple[Optional[str], Optional[RouteGroup]]] = {
    "catalog_reads": (None, CATALOG_READS),
    "products": ("products", PRODUCT_CACHE_READS),
    "users": ("users", None),
    "orders": ("orders", None),
    "admin": ("admin", ADMIN),
    "legal": ("legal", LEGAL),
    "cart": ("cart", CART_ACCOUNT),
    "cart_guest": ("cart", CART_GUEST),
}


async def _heartbeat(stop: asyncio.Event, gaps: List[float]):
    loop = asyncio.get_running_loop()
    last = loop.time()
    while not stop.is_set():
        await asyncio.sleep(HEARTBEAT_SECONDS)
        now = loop.time()
        gaps.append(now - last)
        last = now


async def drive(target_app, requests: List[tuple], concurrency: int, headers=None) -> Measurement:
    """Send `requests` `concurrency` times at once on this loop, next to a
    heartbeat, and measure the longest time the loop went unserved."""
    gaps: List[float] = []
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop, gaps))
    await asyncio.sleep(HEARTBEAT_SECONDS * 3)  # let the heartbeat settle

    async def one_pass(client):
        statuses = []
        for method, path, *rest in requests:
            response = await client.request(method, path, **(rest[0] if rest else {}))
            statuses.append(response.status_code)
        return statuses

    transport = httpx2.ASGITransport(app=target_app)
    started = time.perf_counter()
    try:
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            passes = await asyncio.gather(*(one_pass(client) for _ in range(concurrency)))
    finally:
        wall = time.perf_counter() - started
        stop.set()
        await beat
    return Measurement(
        max_gap=max(gaps, default=0.0),
        wall=wall,
        statuses=[status for statuses in passes for status in statuses],
    )


def _normalise(requests) -> List[tuple]:
    return [request if len(request) > 2 else (*request, {}) for request in requests]


@contextmanager
def poisoned_boundaries(monkeypatch, fake_redis):
    """Synchronous boundaries sleep, asynchronous ones are slow.

    Enter it after seeding: the seeding itself uses the sync session."""
    poison = POISON_SECONDS
    sync_engine = database_module.SessionLocal.kw["bind"]

    def sleep_before_cursor(*args, **kwargs):
        time.sleep(poison)

    event.listen(sync_engine, "before_cursor_execute", sleep_before_cursor)

    def slow_sync(method):
        def wrapper(*args, **kwargs):
            time.sleep(poison)
            return method(*args, **kwargs)

        return wrapper

    for name in ("execute_command", "pipeline", "transaction"):
        monkeypatch.setattr(fake_redis, name, slow_sync(getattr(fake_redis, name)))

    def slow_async(method):
        async def wrapper(*args, **kwargs):
            await asyncio.sleep(LATENCY_SECONDS)
            return await method(*args, **kwargs)

        return wrapper

    for name in ("execute", "scalar", "scalars", "get", "refresh", "commit", "flush"):
        monkeypatch.setattr(AsyncSession, name, slow_async(getattr(AsyncSession, name)))

    make_client = redis_async._create_client

    def slow_client():
        client = make_client()
        client.execute_command = slow_async(client.execute_command)
        return client

    monkeypatch.setattr(redis_async, "_create_client", slow_client)
    try:
        yield
    finally:
        event.remove(sync_engine, "before_cursor_execute", sleep_before_cursor)


def _run(target_app, requests, concurrency, headers=None) -> Measurement:
    async def scenario():
        try:
            return await drive(target_app, requests, concurrency, headers)
        finally:
            await redis_async.close_async_redis()

    return asyncio.run(scenario())


def assert_loop_stays_free(target_app, requests, headers=None, expected_status=200):
    """The check itself: one pass alone for the reference time, then
    CONCURRENCY passes at once."""
    requests = _normalise(requests)
    alone = _run(target_app, requests, 1, headers)
    together = _run(target_app, requests, CONCURRENCY, headers)

    assert set(alone.statuses + together.statuses) == {expected_status}, (
        alone.statuses,
        together.statuses,
    )
    assert together.max_gap < MAX_HEARTBEAT_GAP, (
        f"the event loop was blocked for {together.max_gap:.3f}s "
        f"(a poisoned synchronous call sleeps {POISON_SECONDS}s)"
    )
    assert together.wall < alone.wall * CONCURRENCY * MAX_WALL_FACTOR, (
        f"{CONCURRENCY} concurrent passes took {together.wall:.2f}s against {alone.wall:.2f}s for "
        "one: the requests are being served one after another"
    )


@pytest.mark.parametrize(
    "group",
    [
        pytest.param(
            name,
            marks=pytest.mark.skipif(
                router in PENDING_ROUTERS, reason=f"router '{router}' is not migrated yet"
            ),
        )
        for name, (router, _scenario) in ROUTE_GROUPS.items()
    ],
)
def test_route_group_does_not_block_the_event_loop(group, db_session, fake_redis, monkeypatch):
    _router, scenario = ROUTE_GROUPS[group]
    assert scenario is not None, (
        f"'{group}' is migrated (not in PENDING_ROUTERS) but has no scenario in ROUTE_GROUPS"
    )
    ids = scenario.seed(db_session)
    headers = scenario.headers(db_session) if scenario.headers else None
    requests = scenario.requests(ids)
    db_session.close()

    with poisoned_boundaries(monkeypatch, fake_redis):
        assert_loop_stays_free(app, requests, headers, scenario.expected_status)


# --- The harness itself ---------------------------------------------------------


def _probe_app() -> FastAPI:
    probe = FastAPI()

    @probe.get("/blocking")
    async def blocking():
        time.sleep(POISON_SECONDS)
        return {}

    @probe.get("/yielding")
    async def yielding():
        await asyncio.sleep(POISON_SECONDS)
        return {}

    return probe


def test_the_heartbeat_sees_a_blocked_loop_and_the_wall_time_a_serial_one():
    probe = _probe_app()

    blocking = _run(probe, _normalise([("GET", "/blocking")]), CONCURRENCY)
    yielding = _run(probe, _normalise([("GET", "/yielding")]), CONCURRENCY)

    assert blocking.max_gap >= POISON_SECONDS * 0.9
    assert blocking.wall >= POISON_SECONDS * CONCURRENCY * 0.9
    assert yielding.max_gap < MAX_HEARTBEAT_GAP
    assert yielding.wall < POISON_SECONDS * CONCURRENCY * MAX_WALL_FACTOR


def test_assert_loop_stays_free_fails_for_a_blocking_route_and_passes_for_a_yielding_one():
    probe = _probe_app()

    assert_loop_stays_free(probe, [("GET", "/yielding")])
    with pytest.raises(AssertionError, match="blocked"):
        assert_loop_stays_free(probe, [("GET", "/blocking")])


def test_make_user_headers_logs_a_user_in(client, db_session):
    headers, user = make_user_headers(db_session)

    response = client.get("/api/v1/users/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["data"]["id"] == user.id
