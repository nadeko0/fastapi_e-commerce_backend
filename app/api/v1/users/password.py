import logging

from fastapi import (
    BackgroundTasks,
    HTTPException,
    Request,
    status,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.users._common import _user_by_email
from app.api.v1.users._router import router
from app.core.dependencies import DbSession
from app.core.passwords import BLOCKED_MESSAGE, is_blocked_password
from app.core.rate_limit import clear_login_failures
from app.core.security import (
    async_redis_service,
    generate_password_reset_token,
    get_password_hash_async,
    invalidate_tokens_issued_before_now,
    verify_password_reset_token,
)
from app.models.user import User
from app.schemas.common import Message
from app.schemas.user import (
    PasswordReset,
    PasswordResetConfirm,
    TokenBody,
)
from app.services.email import (
    send_password_reset_email,
)

# The name the module had before the users package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.users")


def _invalid_reset_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired reset token"
    )

@router.post("/password/reset", response_model=Message)
async def request_password_reset(
    reset_request: PasswordReset,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Request a password reset email; always succeeds to avoid leaking registered emails.

    The link is valid for PASSWORD_RESET_TOKEN_EXPIRE_MINUTES (60 by
    default) and only until the password changes. Limited per client address
    by RATE_LIMIT_AUTH (20/minute by default)."""

    user = await _user_by_email(db, reset_request.email)
    if user:
        token = generate_password_reset_token(user.email, user.hashed_password)
        logger.info("Queuing password reset email for user %s", user.id)
        background_tasks.add_task(
            send_password_reset_email,
            user.email,
            token
        )
        logger.info("Password reset email task queued for user %s", user.id)


    return Message(message="If the email exists, a password reset link will be sent")

@router.post(
    "/password/reset-token/validate",
    response_model=Message,
    responses={
        400: {"description": "Invalid or expired reset token"},
        404: {"description": "User not found"},
    },
)
async def validate_reset_token_by_body(
    payload: TokenBody,
    db: DbSession,
):
    """Check whether a password reset token, sent in the body, is still valid.
    It changes nothing. A token is not valid after PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    (60 by default) or once the password has been changed. Limited per client
    address by RATE_LIMIT_AUTH (20/minute by default)."""

    return await _check_reset_token(payload.token, db)


async def _check_reset_token(token: str, db: AsyncSession):
    claims = verify_password_reset_token(token)
    if not claims:
        raise _invalid_reset_token()
    email = claims.email

    user = await _user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    if not claims.matches(user.hashed_password):
        # Already used, or the password has changed since it was issued.
        raise _invalid_reset_token()

    # No email in the answer: whoever holds the token (it was in an emailed
    # link) must not learn whose account it opens just by checking it.
    return Message(message="Token is valid")

@router.post(
    "/password/reset/confirm",
    response_model=Message,
    responses={
        400: {"description": "Invalid or expired reset token"},
        404: {"description": "User not found"},
        422: {"description": "Invalid new password (8 characters to 72 bytes, not a commonly used password or "
                             "one derived from the account's email)"},
        503: {"description": "Old tokens could not be revoked (Retry-After: 5), nothing changed; retry with "
                             "the same token"},
    },
)
async def reset_password(
    payload: PasswordResetConfirm,
    request: Request,
    db: DbSession,
):
    """Reset a user's password using a valid reset token, both sent in the body.

    The token is single use and valid for PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    (60 by default). A reset signs the account out on every device and clears
    its failed-login counters. Limited per client address by RATE_LIMIT_AUTH
    (20/minute by default)."""

    claims = verify_password_reset_token(payload.token)
    if not claims:
        raise _invalid_reset_token()
    email = claims.email

    user = await _user_by_email(db, email)
    user_id = user.id if user else None
    old_hashed_password = user.hashed_password if user else None

    # Same reasoning as login(): release the connection before the
    # CPU-bound bcrypt hash, not after, so the queueing delay of the
    # hash cannot hold it. See the comment in login(). Before the
    # refusals below as well: a refused request would otherwise keep its
    # connection until dependency teardown.
    await db.close()

    if user_id is None or old_hashed_password is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    # The token is single-use: it is only good while the password is
    # the one it was issued against (see PasswordResetClaims in
    # app/core/security.py).
    if not claims.matches(old_hashed_password):
        raise _invalid_reset_token()

    if is_blocked_password(payload.new_password, email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=BLOCKED_MESSAGE,
        )

    new_hashed_password = await get_password_hash_async(payload.new_password)

    # Invalidate any token issued before this reset so a token held by
    # an attacker (the reason the user is resetting) stops working
    # immediately instead of remaining valid until it expires. Before
    # the password changes, and refused if it cannot be recorded: this
    # used to run afterwards with its result ignored, so a Redis outage
    # answered "reset successfully" while every old token kept working.
    # Now nothing has changed when it fails, and the same link can be
    # used again once Redis is back. After the bcrypt hash rather than
    # before it, so that a login with the old password during the hash
    # does not mint a token newer than the watermark.
    if not await invalidate_tokens_issued_before_now(user_id):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset is temporarily unavailable, please retry",
            headers={"Retry-After": "5"},
        )

    # Only if the password is still the one the token was checked
    # against: two requests presenting the same token both pass that
    # check above, and only the first to get here may use it.
    result = await db.execute(
        update(User)
        .where(User.id == user_id, User.hashed_password == old_hashed_password)
        .values(hashed_password=new_hashed_password)
        .execution_options(synchronize_session=False)
    )
    updated = result.rowcount  # type: ignore[attr-defined]  # execute() on UPDATE returns CursorResult at runtime
    await db.commit()
    if updated != 1:
        raise _invalid_reset_token()

    # Evict sessions that raced the reset: a second watermark after the
    # commit refuses tokens a login or refresh rotation minted while the
    # hash was changing, and every refresh family is ended. Best effort:
    # the password has already changed, the first watermark stands.
    if not await invalidate_tokens_issued_before_now(user_id):
        logger.error("Password reset of user %s: second watermark not recorded", user_id)
    if not await async_redis_service.revoke_user_refresh_families(user_id):
        logger.error("Password reset of user %s: refresh families not revoked", user_id)

    # The way out of a login refused for too many failed attempts: the
    # new password starts with a clean record - the email's own count
    # and its count at this address (other addresses' counts are their
    # failures, and lock only them). After the commit, so no database
    # connection is held across it.
    await clear_login_failures(email, request)

    return Message(message="Password has been reset successfully")
