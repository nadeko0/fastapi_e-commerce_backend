from typing import List
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl, validator, PositiveInt

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
    
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors_origins(cls, v: str | List[str]) -> List[AnyHttpUrl]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    DATABASE_URI: str = None
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
    REDIS_PASSWORD: str = None
    REDIS_DB: int = 0
    REDIS_POOL_SIZE: int = 50
    REDIS_POOL_TIMEOUT: int = 20

    REDIS_CART_TTL_DAYS: int = 7
    REDIS_PRODUCT_CACHE_TTL_HOURS: int = 1
    REDIS_JWT_BLACKLIST_TTL_DAYS: int = 7
    REDIS_SESSION_TTL_HOURS: int = 24

    RATE_LIMIT_PER_MINUTE: int = 100

    USER_DATA_RETENTION_DAYS: int = 365
    INACTIVE_ACCOUNT_DELETE_DAYS: int = 730
    GDPR_EXPORT_EXPIRY_HOURS: int = 24
    CONSENT_REQUIRED: bool = True
    DATA_ENCRYPTION_KEY: str

    SMTP_HOST: str
    SMTP_PORT: int = 587
    SMTP_USER: str
    SMTP_PASSWORD: str
    EMAILS_FROM_EMAIL: str
    EMAILS_FROM_NAME: str
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 48

    class Config:
        case_sensitive = True
        env_file = ".env"

settings = Settings()