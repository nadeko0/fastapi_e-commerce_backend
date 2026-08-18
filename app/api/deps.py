from typing import Generator

from app.core.database import SessionLocal


# Authentication lives only in app.core.security (get_current_user,
# get_current_active_user, get_current_admin_user). This module used to
# carry a second, parallel get_current_user that never checked the Redis
# token blacklist and never validated token type, so a blacklisted
# (logged-out) access token - or even a refresh/reset token - could still
# authenticate. It also referenced a non-existent User.is_superuser
# attribute. Removed rather than fixed in place: two auth implementations
# is the underlying bug.
def get_db() -> Generator:
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()
