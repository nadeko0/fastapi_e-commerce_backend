from typing import Annotated, ClassVar, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, model_validator, validator
from pydantic_settings import BaseSettings, NoDecode

from app import __version__
from app.core.currencies import (
    STRIPE_PRESENTMENT_CURRENCIES,
    THREE_DECIMAL_CURRENCIES,
    WHOLE_UNITS_ONLY_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
)


def _split_comma_separated(v: str | List[str]) -> List[str]:
    # Shared by every plain-string-list env setting (TRUSTED_PROXIES,
    # ESSENTIAL_COOKIES): see the longer NoDecode explanation on
    # BACKEND_CORS_ORIGINS below - same underlying pydantic-settings
    # behavior, same fix.
    if isinstance(v, str):
        if not v.strip():
            return []
        if v.startswith("["):
            import json
            return json.loads(v)
        return [i.strip() for i in v.split(",") if i.strip()]
    return v


# A rate limit setting: "N/period" with period second, minute, hour or day,
# optionally a multiple of it: "300/minute", "5/second", "10/15 minutes".
# Groups: count, multiple (optional), unit. Parsed by parse_rate in
# app/core/rate_limit.py.
RATE_LIMIT_PATTERN = r"^([1-9]\d*)/(?:([1-9]\d*) ?)?(second|minute|hour|day)s?$"


# Secrets are checked (Settings.check_deployment_settings) everywhere but here: on a
# developer's machine and in the test suite the .env.example placeholders
# are fine.
_UNDEPLOYED_ENVIRONMENTS = frozenset({"development", "test"})
# An HMAC key at least as long as the hash it keys (RFC 7518 3.2). 64 bytes,
# what .env.example's `openssl rand -hex 32` produces, satisfies all three.
_JWT_KEY_MIN_BYTES = {"HS256": 32, "HS384": 48, "HS512": 64}
# What the values in .env.example and the built-in Stripe defaults are made
# of. A secret containing one of these was copied, not generated.
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "placeholder")
# The repository's own mock Stripe values, whatever follows the prefix.
_PLACEHOLDER_PREFIXES = ("sk_test_mock", "whsec_mock")


def _looks_like_placeholder(value: str) -> bool:
    folded = value.casefold()
    return folded.startswith(_PLACEHOLDER_PREFIXES) or any(
        marker in folded for marker in _PLACEHOLDER_MARKERS
    )


class Settings(BaseSettings):
    PROJECT_NAME: str = "E-commerce Backend"
    # Read from pyproject.toml (see app/__init__.py). ClassVar, not a field,
    # so a VERSION environment variable cannot override it.
    VERSION: ClassVar[str] = __version__
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4
    RELOAD: bool = False

    # Where the API is reached from outside, e.g. "https://shop.example.com":
    # every link in an email (verification, password reset) starts with it.
    # Must be https anywhere but development, since those links carry
    # tokens. Unset in development means http://localhost:{PORT}; unset
    # anywhere else stops the application at startup - the localhost
    # default must never quietly become the link a customer gets.
    PUBLIC_BASE_URL: Optional[str] = None

    @validator("PUBLIC_BASE_URL", always=True)
    def validate_public_base_url(cls, v: Optional[str], values: dict) -> Optional[str]:
        if v is None:
            # Only where nothing is deployed may links point at this machine.
            # A deployment gets no default: check_deployment_settings refuses
            # to start without an https URL.
            if values.get("ENVIRONMENT") in _UNDEPLOYED_ENVIRONMENTS:
                return f"http://localhost:{values.get('PORT', 8000)}"
            return None
        return v.rstrip("/")

    @validator("WORKERS", pre=True)
    def validate_workers(cls, v: int, values: dict) -> int:
        if values.get("ENVIRONMENT") == "development":
            return 1
        return v

    @validator("DEBUG", "RELOAD", pre=True)
    def set_debug_settings(cls, v: bool, values: dict) -> bool:
        if values.get("ENVIRONMENT") == "development":
            return True
        return False

    # NoDecode: pydantic-settings' default env-var handling for any "complex"
    # (list/dict/etc) field type is to run the raw env string through
    # json.loads() *before* pydantic validation ever sees it, regardless of
    # whether the value looks like JSON. Without NoDecode, both an empty
    # string and .env.example's own documented comma-separated format
    # (BACKEND_CORS_ORIGINS=http://a.com,http://b.com) raised a raw
    # json.JSONDecodeError/SettingsError at process startup, before the
    # validator below - which exists specifically to handle the
    # comma-separated case - ever ran. Confirmed by actually deploying the
    # Docker image, not just running the local test suite (which never sets
    # this env var and so never exercised the parsing path at all).
    BACKEND_CORS_ORIGINS: Annotated[List[AnyHttpUrl], NoDecode] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors_origins(cls, v: str | List[str]) -> List[str]:
        return _split_comma_separated(v)

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    # bcrypt cost factor. 12 (bcrypt's own default) is the deliberately slow,
    # brute-force-resistant setting production must use. Tests don't need
    # that security margin and pay real wall-clock cost for it (~400ms per
    # hash at 12 rounds) - tests/conftest.py overrides this via
    # BCRYPT_ROUNDS=4 to keep the suite fast without weakening production.
    BCRYPT_ROUNDS: int = 12
    # How many bcrypt operations one worker process runs at the same time
    # (the rest wait their turn without blocking anything else). Unset keeps
    # the built-in default of 4 x CPU count per worker - see
    # _get_bcrypt_semaphore in app/core/security.py. With several workers
    # that default multiplies: 4 workers on 4 cores admit 64 hashes at once,
    # far more than the cores can run, so a burst of logins slows every
    # request on the machine. Set it to about CPU count / WORKERS to keep
    # bcrypt from taking more than the cores there are.
    BCRYPT_MAX_CONCURRENCY: Optional[int] = Field(None, ge=1)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # How long a password-reset link works. It was 24 hours, hard-coded: a
    # day for a link that travels in a URL (proxy logs, browser history,
    # Referer) to be found and used. An hour is enough to open an email;
    # a new link is one request away.
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = Field(60, ge=1)

    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    DATABASE_URI: Optional[str] = None
    SQL_DEBUG: bool = False

    DB_POOL_SIZE: int = 10
    # 10 + 5 per engine. There are two engines per worker (sync and async,
    # app/core/database.py), so a worker can open up to 30 connections and
    # four workers 120: size Postgres' max_connections for WORKERS x 2 x
    # (DB_POOL_SIZE + DB_MAX_OVERFLOW), plus Celery. docker-compose.yml sets
    # 200. The overflow used to be 20 (60 per worker), which four workers
    # could not fit into Postgres' default of 100; a load test hit exactly
    # that ("too many clients already").
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_PRE_PING: bool = True
    DB_POOL_RECYCLE: int = 3600

    @validator("DB_POOL_SIZE", "DB_MAX_OVERFLOW", pre=True)
    def adjust_pool_size(cls, v: str | int, values: dict) -> int:
        v_int = int(v) if isinstance(v, str) else v
        if values.get("ENVIRONMENT") == "development":
            return max(5, v_int // 2)
        return v_int

    @validator("SQL_DEBUG", pre=True)
    def set_sql_debug(cls, v: bool, values: dict) -> bool:
        return values.get("DEBUG", False)

    @validator("DATABASE_URI", pre=True)
    def assemble_db_connection(cls, v: str | None, values: dict) -> str:
        if isinstance(v, str):
            return v
        return f"postgresql+psycopg://{values.get('POSTGRES_USER')}:{values.get('POSTGRES_PASSWORD')}@{values.get('POSTGRES_SERVER')}/{values.get('POSTGRES_DB')}"

    REDIS_HOST: str
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_DB: int = 0
    REDIS_POOL_SIZE: int = 50
    REDIS_POOL_TIMEOUT: int = 20

    # How long a cart lives after its last write. The only source of the
    # TTL (RedisService._cart_ttl). It was 7 while a cart displayed the
    # prices recorded when its lines were added, to bound how stale those
    # could get; the cart is now priced from the catalog on every request,
    # so it can be kept for as long as is useful to the customer.
    REDIS_CART_TTL_DAYS: int = 30
    # The same for the cart of a visitor who is not logged in: how long it
    # lives after its last write, and the only source of that TTL. It is
    # also the Max-Age of the cookie carrying the cart's identifier, so the
    # two expire together. Shorter than an account's cart on purpose: anyone
    # can create one without an account, it is reachable by whoever holds
    # its identifier, and it only has to last until the visitor logs in.
    REDIS_GUEST_CART_TTL_DAYS: int = Field(7, ge=1)
    # The most distinct lines (product, or product + variant) a cart can
    # hold, for an account and for a guest alike. It bounds the size of the
    # Redis value and of the IN list in the catalog query that prices a
    # cart, both of which an anonymous caller could otherwise grow without
    # limit. Far above what a real basket contains; quantities per line are
    # bounded by stock, not by this.
    CART_MAX_LINES: int = Field(100, ge=1)
    # Whether the guest cart cookie is sent with the Secure attribute (and,
    # with it, under the __Host- name prefix). True everywhere a browser
    # reaches the API over https, which is everywhere but a developer's
    # machine: set it to false only to work over plain http locally.
    GUEST_CART_COOKIE_SECURE: bool = True
    REDIS_PRODUCT_CACHE_TTL_HOURS: int = 1
    REDIS_JWT_BLACKLIST_TTL_DAYS: int = 7
    REDIS_SESSION_TTL_HOURS: int = 24

    # Rate limiting settings
    # Reverse proxies/load balancers allowed to set X-Forwarded-For: single
    # addresses ("10.0.0.5") and networks in CIDR form ("10.0.0.0/8",
    # "fd00::/8"), comma-separated. Empty by default: with no trusted proxy
    # in front, X-Forwarded-For is attacker-controlled and is ignored in
    # favor of the socket peer address. When the peer is trusted, the client
    # is the rightmost X-Forwarded-For entry that is not itself trusted (see
    # client_address in app/core/rate_limit.py). Behind a proxy this MUST be
    # set, or every visitor shares the proxy's rate-limit counters.
    TRUSTED_PROXIES: Annotated[List[str], NoDecode] = []

    @validator("TRUSTED_PROXIES", pre=True)
    def split_trusted_proxies(cls, v: str | List[str]) -> List[str]:
        return _split_comma_separated(v)
    # Per worker process. Unset (the default) means no limit, which is the
    # behaviour this app always had. When set, a request arriving while this
    # many are already in progress gets an immediate 503 instead of joining
    # them - see app/core/overload.py for why that matters here and how to
    # size it against DB_POOL_SIZE + DB_MAX_OVERFLOW.
    MAX_IN_FLIGHT_REQUESTS: Optional[int] = None

    # Kill switch for everything in app/core/rate_limit.py: the per-request
    # limits and the failed-login counter. Off means no request is ever
    # answered 429 by it and Redis is not asked.
    RATE_LIMIT_ENABLED: bool = True
    # The limits, as "N/period" (RATE_LIMIT_PATTERN above; a value that does
    # not match stops the application at startup). Each is counted per
    # operation (method + route template) and per caller. Which route uses
    # which: _ROUTE_LIMITS in app/core/rate_limit.py. Mirrored in
    # .env.example.
    #
    # Every route not named below. Per user when the request carries a
    # valid access token, otherwise per client address.
    RATE_LIMIT_DEFAULT: str = Field("300/minute", pattern=RATE_LIMIT_PATTERN)
    # Product search, the costliest read. Per user or address, as above.
    RATE_LIMIT_SEARCH: str = Field("60/minute", pattern=RATE_LIMIT_PATTERN)
    # Register, login, password-reset request, reset/verification token
    # submit, verification resend. Always per client address.
    RATE_LIMIT_AUTH: str = Field("20/minute", pattern=RATE_LIMIT_PATTERN)
    # Creating an order, paying, opening a checkout session. Per user.
    RATE_LIMIT_CHECKOUT: str = Field("10/minute", pattern=RATE_LIMIT_PATTERN)
    # Failed logins, two counts (see "Failed logins" in app/core/rate_limit.py).
    # A successful login or a completed password reset clears them.
    # The hard lock: failed logins for one email from one client address.
    # After this many in the period, that address cannot log in to that
    # email until the period is over; the owner, elsewhere, still can.
    RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS: str = Field(
        "10/15 minutes", pattern=RATE_LIMIT_PATTERN
    )
    # The backstop against guessing spread over many addresses: failed
    # logins for one email from anywhere. Far looser than the hard lock, so
    # that locking an owner out takes many addresses, not one.
    RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL: str = Field("100/hour", pattern=RATE_LIMIT_PATTERN)
    # Renamed: it was the per-email count, which let one address lock any
    # account. Refused rather than silently ignored, so that a deployment
    # still setting it learns that its value no longer applies.
    RATE_LIMIT_LOGIN_FAILURES: Optional[str] = None

    @validator("RATE_LIMIT_LOGIN_FAILURES")
    def reject_renamed_login_failures(cls, v: Optional[str]) -> None:
        if v is not None:
            raise ValueError(
                "RATE_LIMIT_LOGIN_FAILURES was split into "
                "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS and "
                "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL; set those instead"
            )
        return None
    # New guest carts per client address. Not a limit on requests: adding
    # to a cart that already exists is not counted, only the add that has
    # to open a new one (app/api/v1/cart.py). Each new cart is a Redis key
    # that lives for REDIS_GUEST_CART_TTL_DAYS, so without this a client
    # that never sends the cookie back leaves one behind per request. A
    # visitor needs one; the margin is for many visitors behind one address
    # (an office, a carrier's NAT).
    RATE_LIMIT_GUEST_CART: str = Field("100/hour", pattern=RATE_LIMIT_PATTERN)

    # Essential GDPR Settings
    USER_DATA_RETENTION_DAYS: int = 365
    CONSENT_REQUIRED: bool = True
    # There is no DATA_ENCRYPTION_KEY: the application encrypts no field
    # itself (personal data is protected by the database volume's disk
    # encryption and TLS to the database). One used to be required at
    # startup and was never used, which suggested otherwise. A leftover
    # line in an old .env is ignored like any unknown variable.
    GDPR_EXPORT_EXPIRY_HOURS: int = 24
    # The most records GET /users/data/export assembles in one response:
    # addresses + orders + order items of the account. The export is built
    # in the worker's memory and sent whole; past this it is refused with a
    # pointer to DPO_EMAIL instead. Far above any real customer's history
    # (an order holds at most CART_MAX_LINES items).
    GDPR_EXPORT_MAX_RECORDS: int = Field(5000, ge=1)
    INACTIVE_ACCOUNT_DELETE_DAYS: int = 730  # 2 years
    # Grace period between a user requesting erasure (Art. 17) and the
    # scheduled hard delete. Distinct from INACTIVE_ACCOUNT_DELETE_DAYS,
    # which is unrelated general staleness cleanup, not an erasure request.
    DATA_DELETION_GRACE_PERIOD_DAYS: int = 1

    # Company Information (MVP)
    COMPANY_NAME: str = "Your Company Name"
    COMPANY_EMAIL: str = "contact@yourcompany.com"
    COMPANY_ADDRESS: str = "Your Company Address, EU"  # Required for EU compliance
    COMPANY_VAT: str = "EU VAT Number"  # Required for EU business
    DPO_NAME: str = "Data Protection Officer"
    DPO_EMAIL: str = "dpo@yourcompany.com"
    TECHNICAL_CONTACT: str = "Technical Support"
    # Recipient for the check_low_stock Celery task's alert email
    # (app/tasks.py) - referenced there but never defined here, so that
    # task raised AttributeError every time it ran (scheduled every 4h).
    ADMIN_EMAIL: str = "admin@yourcompany.com"

    # Cookie Consent Settings
    COOKIE_CONSENT_ENABLED: bool = True
    COOKIE_CONSENT_EXPIRE_DAYS: int = 365
    ESSENTIAL_COOKIES: Annotated[List[str], NoDecode] = [
        "session", "csrf_token", "__Host-guest_cart",
    ]

    @validator("ESSENTIAL_COOKIES", pre=True)
    def split_essential_cookies(cls, v: str | List[str]) -> List[str]:
        return _split_comma_separated(v)

    # GDPR Email Templates
    GDPR_EXPORT_READY_TEMPLATE: str = "gdpr_export_ready"
    GDPR_DELETION_CONFIRMED_TEMPLATE: str = "gdpr_deletion_confirmed"
    GDPR_REQUEST_RECEIVED_TEMPLATE: str = "gdpr_request_received"

    # Email Settings
    SMTP_HOST: str
    SMTP_PORT: int = 587
    SMTP_USER: str
    SMTP_PASSWORD: str
    SMTP_TIMEOUT_SECONDS: int = 10
    EMAILS_FROM_EMAIL: str
    EMAILS_FROM_NAME: str
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 48
    # Which EmailProvider app.services.email.get_email_provider() hands out:
    # "smtp" actually sends via SMTP_HOST; "logging" (or anything else) only
    # logs and never touches the network - used by tests/local dev so they
    # don't need a real SMTP server.
    EMAIL_PROVIDER: str = "smtp"

    # Which PaymentProvider app.services.payment.get_payment_provider()
    # hands out: "mock" is the in-process StripePaymentProvider, which
    # never moves money (it confirms with a successful test card when no
    # card token is sent); "stripe" is LiveStripePaymentProvider, real
    # calls to api.stripe.com with STRIPE_SECRET_KEY. Production refuses
    # the mock (check_deployment_settings below).
    PAYMENT_PROVIDER: Literal["mock", "stripe"] = "mock"
    # The defaults are the repository's placeholders, published in
    # .env.example: fine for the mock, which makes no network call and is
    # only ever sent webhooks signed by the tests, and refused in
    # production, where anyone who has read the repository could sign a
    # webhook with them.
    STRIPE_SECRET_KEY: str = "sk_test_mock_placeholder"
    STRIPE_WEBHOOK_SECRET: str = "whsec_mock_placeholder"
    STRIPE_WEBHOOK_TOLERANCE_SECONDS: int = 300
    # The one currency the store prices and charges in. Product prices carry
    # no currency of their own; each order records this value when it is
    # placed (Order.currency) and is only ever charged in it, whatever a
    # payment request says. Amounts are converted to the smallest unit by
    # multiplying by 100 (_decimal_to_cents in app/api/v1/orders.py) and
    # prices are stored with two decimals, so only a currency with exactly
    # two decimals works: validate_store_currency refuses the others.
    STORE_CURRENCY: str = Field("USD", pattern=r"^[A-Z]{3}$")
    # Where the storefront is served: scheme, host and port, no trailing
    # slash. A Checkout Session sends the customer back here after Stripe's
    # hosted page; success_url/cancel_url from a client are accepted only
    # on this origin, and built on it when omitted (app/schemas/order.py,
    # create_checkout_session). The default is a local frontend; production
    # requires https (check_payment_settings_for_production).
    FRONTEND_URL: str = "http://localhost:3000"
    # How long an order may stay unpaid before the cancel_expired_unpaid_orders
    # task (app/tasks.py, every 5 minutes) cancels it and returns its stock.
    # Stock is taken when the order is placed, so until then those units
    # cannot be sold to anyone else. Long enough for a customer to finish
    # paying, including 3D Secure; a payment that completes after expiry is
    # flagged for manual review (a refund), not applied.
    UNPAID_ORDER_EXPIRE_MINUTES: int = Field(30, ge=1)
    # How long a payment attempt whose outcome is not known yet (talking to
    # the provider, in 3D Secure, an open Checkout Session) keeps another
    # attempt for the same order from starting (409). Long enough for 3D
    # Secure; short enough that an attempt left behind by a crashed request,
    # or a Checkout Session the customer walked away from, does not keep
    # the order from being paid.
    PAYMENT_ATTEMPT_STALE_AFTER_MINUTES: int = Field(5, ge=1)

    # Daily reconciliation of our payments against the provider's
    # (app/services/reconciliation.py). It looks at payments created in the
    # last RECONCILIATION_WINDOW_DAYS days, except those younger than
    # RECONCILIATION_MIN_AGE_MINUTES (an attempt still being paid or
    # confirmed by webhook is not a difference yet). At most
    # RECONCILIATION_MAX_PAYMENTS are checked per run (newest first), two
    # provider calls each, so a run stays inside the Celery time limit and
    # the provider's rate limit; the run stops early after
    # RECONCILIATION_MAX_CONSECUTIVE_ERRORS provider failures in a row
    # (the provider is down or rate limiting us; the next run tries again).
    RECONCILIATION_WINDOW_DAYS: int = Field(7, ge=1)
    RECONCILIATION_MIN_AGE_MINUTES: int = Field(60, ge=0)
    RECONCILIATION_MAX_PAYMENTS: int = Field(200, ge=1)
    RECONCILIATION_MAX_CONSECUTIVE_ERRORS: int = Field(5, ge=1)

    @validator("FRONTEND_URL")
    def validate_frontend_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
            raise ValueError("FRONTEND_URL must be an http(s) URL with a host")
        return v.rstrip("/")

    # In every environment, not only in a deployment: with JPY a 1000 yen
    # order was sent to the provider as 100000 (yen, Stripe's smallest JPY
    # unit), with KWD as a tenth of its price. Nothing in between notices.
    @validator("STORE_CURRENCY")
    def validate_store_currency(cls, v: str) -> str:
        # Shaped like a currency is not enough: "ABC" matches the pattern
        # and the application would start, then Stripe would refuse every
        # payment in it.
        if v not in STRIPE_PRESENTMENT_CURRENCIES:
            raise ValueError(
                f"STORE_CURRENCY {v} is not a currency Stripe accepts for payments "
                "(https://docs.stripe.com/currencies)"
            )
        if v in WHOLE_UNITS_ONLY_CURRENCIES:
            raise ValueError(
                f"STORE_CURRENCY {v} can only be charged in whole units; prices "
                "here have two decimal places, so it is not supported"
            )
        if v in ZERO_DECIMAL_CURRENCIES or v in THREE_DECIMAL_CURRENCIES:
            decimals = "no" if v in ZERO_DECIMAL_CURRENCIES else "three"
            raise ValueError(
                f"STORE_CURRENCY {v} has {decimals} decimals; only currencies with two "
                "decimal places are supported (amounts are charged in hundredths)"
            )
        return v

    @model_validator(mode="after")
    def check_deployment_settings(self) -> "Settings":
        # Anything but development and test is a deployment, including a
        # misspelt name: failing closed costs a restart, failing open a
        # forgeable token or orders "paid" without money. Every problem is
        # reported at once, by setting name only: the message ends up in
        # logs, the values must not.
        if self.ENVIRONMENT in _UNDEPLOYED_ENVIRONMENTS:
            return self
        problems = []
        # The mock provider marks every order paid without taking any money.
        if self.PAYMENT_PROVIDER != "stripe":
            problems.append("PAYMENT_PROVIDER must be 'stripe' outside development and test")
        for name, minimum_bytes in (
            ("JWT_SECRET_KEY", _JWT_KEY_MIN_BYTES.get(self.JWT_ALGORITHM, 32)),
            ("STRIPE_SECRET_KEY", 0),
            ("STRIPE_WEBHOOK_SECRET", 32),
            ("POSTGRES_PASSWORD", 16),
            ("REDIS_PASSWORD", 16),
            # Issued by the mail provider, so no length of our choosing.
            ("SMTP_PASSWORD", 0),
        ):
            value = getattr(self, name) or ""
            if not value:
                problems.append(f"{name} is not set")
            elif _looks_like_placeholder(value):
                problems.append(f"{name} is a placeholder, not a secret")
            elif len(value.encode("utf-8")) < minimum_bytes:
                problems.append(f"{name} is shorter than {minimum_bytes} bytes")
        # Paying customers are sent back here from Stripe Checkout.
        if not self.FRONTEND_URL.startswith("https://"):
            problems.append("FRONTEND_URL must be an https:// URL")
        # Verification and password-reset links in emails point here.
        if not (self.PUBLIC_BASE_URL or "").startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be set to an https:// URL")
        if problems:
            raise ValueError(
                f"ENVIRONMENT={self.ENVIRONMENT!r} is not deployable: " + "; ".join(problems)
            )
        return self

    class Config:
        case_sensitive = True
        env_file = ".env"
        # docker-compose.yml's api service loads the whole .env file via
        # env_file, which also contains docker-compose-only variables
        # (DOCKER_POSTGRES_PORT, DOCKER_REDIS_PORT, DOCKER_NETWORK_NAME -
        # used for host port mapping/network naming, not read by the app).
        # pydantic-settings' BaseSettings defaults to forbidding unknown
        # fields, so those leaked straight through as a startup-crashing
        # ValidationError. Confirmed by actually deploying the Docker
        # image, not just running the local test suite (which sets only
        # the exact env vars Settings expects and so never exercised this
        # path)
        extra = "ignore"
        # A validation error otherwise ends with a repr of the whole input,
        # secrets included, and is printed at startup into the logs.
        hide_input_in_errors = True

settings = Settings()
