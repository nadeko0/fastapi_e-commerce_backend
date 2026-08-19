from typing import Annotated, List, Optional

from pydantic import AnyHttpUrl, validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    PROJECT_NAME: str = "E-commerce Backend"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4
    RELOAD: bool = False

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
    def assemble_cors_origins(cls, v: str | List[str]) -> List[AnyHttpUrl]:
        if isinstance(v, str):
            if not v.strip():
                return []
            if v.startswith("["):
                import json
                return json.loads(v)
            return [i.strip() for i in v.split(",") if i.strip()]
        elif isinstance(v, list):
            return v
        raise ValueError(v)

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    # bcrypt cost factor. 12 (bcrypt's own default) is the deliberately slow,
    # brute-force-resistant setting production must use. Tests don't need
    # that security margin and pay real wall-clock cost for it (~400ms per
    # hash at 12 rounds) - tests/conftest.py overrides this via
    # BCRYPT_ROUNDS=4 to keep the suite fast without weakening production.
    BCRYPT_ROUNDS: int = 12
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    DATABASE_URI: Optional[str] = None
    SQL_DEBUG: bool = False

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
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
        return f"postgresql://{values.get('POSTGRES_USER')}:{values.get('POSTGRES_PASSWORD')}@{values.get('POSTGRES_SERVER')}/{values.get('POSTGRES_DB')}"

    REDIS_HOST: str
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_DB: int = 0
    REDIS_POOL_SIZE: int = 50
    REDIS_POOL_TIMEOUT: int = 20

    REDIS_CART_TTL_DAYS: int = 7
    REDIS_PRODUCT_CACHE_TTL_HOURS: int = 1
    REDIS_JWT_BLACKLIST_TTL_DAYS: int = 7
    REDIS_SESSION_TTL_HOURS: int = 24

    # Rate limiting settings
    # IPs of reverse proxies/load balancers allowed to set X-Forwarded-For.
    # Empty by default: with no trusted proxy in front, X-Forwarded-For is
    # attacker-controlled and must be ignored in favor of the socket peer IP.
    TRUSTED_PROXIES: List[str] = []
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_ANONYMOUS: int = 30  # requests per minute
    RATE_LIMIT_AUTHENTICATED: int = 60  # requests per minute
    RATE_LIMIT_ADMIN: int = 120  # requests per minute
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    # Special endpoint rate limits (requests per minute)
    RATE_LIMIT_LOGIN: int = 5
    RATE_LIMIT_REGISTER: int = 3
    RATE_LIMIT_PRODUCTS: int = 100

    # Essential GDPR Settings
    USER_DATA_RETENTION_DAYS: int = 365
    CONSENT_REQUIRED: bool = True
    DATA_ENCRYPTION_KEY: str
    GDPR_EXPORT_EXPIRY_HOURS: int = 24
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
    ESSENTIAL_COOKIES: List[str] = ["session", "csrf_token"]

    # GDPR Email Templates
    GDPR_EXPORT_READY_TEMPLATE: str = "gdpr_export_ready"
    GDPR_DELETION_CONFIRMED_TEMPLATE: str = "gdpr_deletion_confirmed"
    GDPR_REQUEST_RECEIVED_TEMPLATE: str = "gdpr_request_received"

    # Email Settings
    SMTP_HOST: str
    SMTP_PORT: int = 587
    SMTP_USER: str
    SMTP_PASSWORD: str
    EMAILS_FROM_EMAIL: str
    EMAILS_FROM_NAME: str
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 48

    # Payment settings (mock Stripe integration - no real Stripe account
    # exists for this project; these are placeholder values, never real
    # secrets, and no outbound network call is ever made with them).
    STRIPE_SECRET_KEY: str = "sk_test_mock_placeholder"
    STRIPE_WEBHOOK_SECRET: str = "whsec_mock_placeholder"
    STRIPE_WEBHOOK_TOLERANCE_SECONDS: int = 300

    class Config:
        case_sensitive = True
        env_file = ".env"

settings = Settings()
