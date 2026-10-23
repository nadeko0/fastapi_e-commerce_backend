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
#
# get_db below is no longer what those auth dependencies use (they load the
# user through app.core.database.get_async_db), so a route depending on it
# does not share a session with them: current_user is a detached object, and
# a route that changes the user loads the row through its own session - see
# _load_user in app/api/v1/legal.py, the only router still on this get_db.
def get_db() -> Generator:
    # Created before the try, as in FastAPI's "Dependencies with yield" docs:
    # if SessionLocal() raises, there is no session to close, and a finally
    # that touched an unbound `db` would hide the real error behind an
    # UnboundLocalError.
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
