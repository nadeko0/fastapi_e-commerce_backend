import logging
from contextlib import contextmanager
from typing import Generator

from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.DATABASE_URI,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=3600,
    echo=False,
    echo_pool=False,
    logging_name=None
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False
)

@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        # str(e) on a SQLAlchemy error includes the failing SQL statement
        # and its bound parameter values by default - for an INSERT/UPDATE
        # that's frequently user-submitted data (email, address, etc).
        # Returning that verbatim in a 500 response leaks both PII and
        # internal schema/query details to the client; log it in full
        # server-side instead and return a generic message.
        logger.error("Database error in session_scope", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred"
        )
    finally:
        session.close()

def get_db() -> Generator[Session, None, None]:
    with session_scope() as session:
        try:
            yield session
        except SQLAlchemyError as e:
            logger.error("Database error in get_db", exc_info=e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="A database error occurred"
            )
