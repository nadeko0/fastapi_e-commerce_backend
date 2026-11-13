import logging
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.cart import merge_guest_cart_on_login_async
from app.api.v1.users._common import _user_by_email
from app.api.v1.users._router import router
from app.core.clock import utc_now
from app.core.dependencies import BearerToken, Cache, CurrentUser, DbSession
from app.core.openapi import CLEAR_GUEST_CART_COOKIE_RESPONSE
from app.core.rate_limit import (
    check_login_attempt,
    clear_login_failures,
    login_unavailable,
)
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenError,
    RevocationUnavailable,
    create_access_token,
    generate_email_verification_token,
    get_password_hash_async,
    issue_refresh_token,
    rotate_refresh_token,
    token_check_unavailable,
    verify_email_token,
    verify_password_async,
)
from app.core.security import (
    logout as logout_tokens,
)
from app.models.user import User
from app.schemas.common import Message
from app.schemas.user import (
    ConsentType,
    LogoutRequest,
    RefreshTokenRequest,
    Token,
    TokenBody,
    UserCreate,
)
from app.services.email import (
    send_account_exists_email,
    send_email_verification,
    send_welcome_email,
)
from app.services.gdpr import consent_user_agent
from app.services.redis_async import TokenStateUnavailable

# The name the module had before the users package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.users")


REGISTRATION_RECEIVED = (
    "Registration received. Check your inbox: we have sent you an email "
    "with what to do next."
)

@router.post(
    "/register",
    response_model=Message,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Registration received, whether or not the email already had an account"},
        422: {"description": "Invalid email, name, phone or password (8 characters to 72 bytes, not a commonly "
                             "used password or one derived from the email), or gdpr_consent or "
                             "privacy_policy_accepted is not true"},
    },
)
async def register_user(
    user_in: UserCreate,
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Register a new user account and queue a welcome/verification email.

    Answers 202 (RFC 9110 15.3.3: accepted for processing, the outcome is
    in the email), exactly the same whether or not the email already has an
    account, so that registration cannot be used to find out which emails
    are registered (login and password reset already take care not to).
    For an existing account nothing is created or changed; its owner gets
    a "you already have an account" email instead of the welcome one.

    The welcome email carries a verification link, valid for
    EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS (48 by default). Placing an order
    needs a verified email, a full name and a phone number. Limited per
    client address by RATE_LIMIT_AUTH (20/minute by default)."""
    now = utc_now()
    client_ip = request.client.host if request.client else None
    client_ua = consent_user_agent(request)
    # Hashed in both cases, before the database is asked, so that the
    # answer for an existing email does not come back a bcrypt early.
    hashed_password = await get_password_hash_async(user_in.password)
    user = User(
        email=user_in.email,
        hashed_password=hashed_password,
        full_name=user_in.full_name,
        phone=user_in.phone,
        gdpr_consent=user_in.gdpr_consent,
        gdpr_consent_date=now if user_in.gdpr_consent else None,
        privacy_policy_accepted=user_in.privacy_policy_accepted,
        privacy_policy_accepted_date=now if user_in.privacy_policy_accepted else None,
        marketing_consent=user_in.marketing_consent,
        marketing_consent_date=now if user_in.marketing_consent else None,
        consent_history=[{
            "type": ConsentType.GDPR.value,
            "granted": user_in.gdpr_consent,
            "timestamp": now.isoformat(),
            "ip_address": client_ip,
            "user_agent": client_ua,
        }, {
            "type": ConsentType.PRIVACY_POLICY.value,
            "granted": user_in.privacy_policy_accepted,
            "timestamp": now.isoformat(),
            "ip_address": client_ip,
            "user_agent": client_ua,
        }, {
            "type": ConsentType.MARKETING.value,
            "granted": user_in.marketing_consent,
            "timestamp": now.isoformat(),
            "ip_address": client_ip,
            "user_agent": client_ua,
        }]
    )
    db.add(user)
    new_full_name: str | None = None
    new_user_id: int | None = None
    try:
        await db.commit()
        existing_email = None
        new_email, new_full_name, new_user_id = user.email, user.full_name, user.id
    except IntegrityError:
        # The email (in any case: see uq_users_email_lower) has an account.
        await db.rollback()
        existing = await _user_by_email(db, user_in.email)
        existing_email = existing.email if existing else None
        new_email = None
    # Nothing below touches `db`. Closing here returns the connection now
    # instead of leaving it to FastAPI's post-return dependency teardown -
    # the same early release login()/reset_password() do (see the comment in
    # login()).
    await db.close()

    if new_email:
        verification_token = generate_email_verification_token(new_email)
        logger.info("Queuing welcome email for user %s", new_user_id)
        background_tasks.add_task(
            send_welcome_email,
            new_email,
            new_full_name,
            verification_token
        )
        logger.info("Welcome email task queued for user %s", new_user_id)
    elif existing_email:
        # To the address on the account, which may differ in case from
        # the one typed.
        logger.info("Registration for an existing account; owner notified")
        background_tasks.add_task(send_account_exists_email, existing_email)

    return Message(message=REGISTRATION_RECEIVED)

@router.post(
    "/verify-email",
    response_model=Message,
    responses={
        400: {"description": "Invalid or expired verification token"},
        404: {"description": "User not found"},
    },
)
async def verify_email_by_body(
    payload: TokenBody,
    db: DbSession,
):
    """Confirm a user's email address using a verification token sent in the
    body.

    The token is valid for EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS (48 by
    default); ask for a new one with `POST /users/verify-email/resend`.
    Confirming an address that is already verified is a 200 too. Limited per
    client address by RATE_LIMIT_AUTH (20/minute by default)."""

    return await _confirm_email(payload.token, db)


async def _confirm_email(token: str, db: AsyncSession):
    email = verify_email_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token"
        )

    user = await _user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if user.is_email_verified:
        return Message(message="Email already verified")

    user.is_email_verified = True
    user.email_verification_date = utc_now()
    await db.commit()

    return Message(message="Email verified successfully")

@router.post(
    "/verify-email/resend",
    response_model=Message,
    responses={409: {"description": "Email already verified"}},
)
async def resend_verification_email(
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Resend the email verification link to the current user. The link is
    valid for EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS (48 by default). Limited
    per client address by RATE_LIMIT_AUTH (20/minute by default)."""

    if current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already verified"
        )

    verification_token = generate_email_verification_token(current_user.email)
    logger.info("Queuing verification email resend for user %s", current_user.id)
    background_tasks.add_task(
        send_email_verification,
        current_user.email,
        verification_token
    )
    logger.info("Verification email resend task queued for user %s", current_user.id)

    return Message(message="Verification email sent")

@router.post(
    "/login",
    response_model=Token,
    responses={
        200: CLEAR_GUEST_CART_COOKIE_RESPONSE,
        401: {"description": "Incorrect email or password (also for a deactivated account)"},
        429: {"description": "Too many requests from this address (RATE_LIMIT_AUTH, 20/minute by default), or "
                             "too many failed logins for this email: "
                             "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS (10 per 15 minutes by default, "
                             "from one address) or RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL (100/hour, from anywhere); "
                             "a correct password does not help until Retry-After has passed"},
        503: {"description": "Failed attempts cannot be counted, or the refresh token cannot be recorded "
                             "(Retry-After: 30), retry later"},
    },
)
async def login(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbSession,
    redis: Cache,
):
    """Authenticate with email/password and receive access and refresh tokens.

    If the request carries a guest cart cookie, that cart is merged into the
    account's cart and the cookie is cleared. A cart that cannot be merged
    right now never fails the login.

    Answers 429 with Retry-After when too many attempts for this email have
    failed, from this address or from everywhere (see "Failed logins" in
    app/core/rate_limit.py), and 503 with Retry-After while those attempts
    cannot be counted (Redis down)."""

    # Failed logins are counted per email and address, and per email. Here,
    # before the first query: `db` has not checked a connection out yet, so
    # this await holds none, and the call itself only talks to Redis. When
    # it refuses, nothing else happens - no user lookup, no bcrypt - so a
    # refusal is equally fast, and identical, whether or not the account
    # exists.
    await check_login_attempt(form_data.username, request)

    user = await _user_by_email(db, form_data.username)
    user_id = user.id if user else None
    hashed_password = user.hashed_password if user else None
    # Read here with the others, off the row already loaded, because the
    # session is closed next.
    user_is_active = bool(user.is_active) if user else False

    # Release this request's DB connection back to the pool *before* the
    # CPU-bound bcrypt verification, instead of after. verify_password_async
    # offloads to a shared threadpool (see app/core/security.py) that carries
    # every other concurrent request's bcrypt work too, so under load a
    # connection held open across this await is held for however long that
    # queue is, not just one hash's cost. Closing the AsyncSession returns
    # the checked-out connection to the pool; the session itself stays
    # usable and checks out a fresh connection on its next use below
    # (tests/integration/test_db_pool_concurrency.py proves that none is
    # held during the hash).
    await db.close()

    # Deliberately NOT `user_id is None or not await verify_password_async(...)`:
    # `or` short-circuits, so that structure would skip the bcrypt call
    # entirely whenever the account doesn't exist - the intent of falling
    # back to a dummy hash below only holds if verify_password_async always
    # actually runs. Compute the password check unconditionally first, then
    # combine it with the existence check.
    password_valid = await verify_password_async(
        form_data.password, hashed_password or DUMMY_PASSWORD_HASH
    )
    # A deactivated account (erasure requested, or switched off by an admin)
    # gets no tokens. Refused exactly like a wrong password - same status,
    # same wording, and only after bcrypt has run - so that knowing the
    # password does not reveal that the account exists but is switched off.
    if user_id is None or not password_valid or not user_is_active:
        # The attempt was counted when it started (check_login_attempt), for
        # the email as submitted, account or no account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # A success wipes the email's failed-attempt count. After db.close()
    # above, so no database connection is held across this await.
    await clear_login_failures(form_data.username, request)

    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(last_login=utc_now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    logger.info("Generating tokens for user %s", user_id)
    access_token = create_access_token(user_id)
    try:
        refresh_token = await issue_refresh_token(user_id)
    except TokenStateUnavailable as e:
        # The refresh token could not be recorded, so it would fail at its
        # first use with a 401 and log the user out; hand out neither token.
        # The password was right and nothing is wrong with the account: a
        # 503 the client retries, as for the failed-attempt count above.
        raise login_unavailable() from e
    # A password reset that landed while bcrypt ran (the hash was read
    # before it): these tokens would be newer than the reset's watermark and
    # outlive the reset. Re-read after minting; a reset that commits later
    # is caught by its second watermark (reset_password).
    current_hash = await db.scalar(select(User.hashed_password).where(User.id == user_id))
    await db.close()
    if current_hash != hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.info("Tokens generated for user %s", user_id)

    # The cart the visitor filled before logging in becomes part of the
    # account's cart. Last, and Redis only: the password is verified, the
    # last_login update is committed (so the session holds no connection
    # here, as after the db.close() above) and the tokens exist, and
    # nothing in it can raise - the login succeeds whatever becomes of the
    # cart. Only an active account gets this far (see the refusal above).
    await merge_guest_cart_on_login_async(request, response, user_id, redis)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer"
    )

@router.post(
    "/refresh",
    response_model=Token,
    responses={
        401: {"description": "Invalid, expired, or already-used refresh token (reuse also revokes the "
                             "token's family: log in again), or a session older than "
                             "REFRESH_SESSION_MAX_DAYS (log in again)"},
        503: {"description": "The token cannot be checked right now (Retry-After: 30); keep the refresh "
                             "token and retry"},
    },
)
async def refresh_access_token(
    refresh_in: RefreshTokenRequest,
    db: DbSession,
):
    """Exchange a refresh token for a new access token, rotating the refresh
    token in the process. Does not require a valid (or even present) access
    token - that's the whole point of a refresh token. The presented refresh
    token is single-use: redeeming it a second time is treated as a
    stolen-token signal and revokes the rest of its token family, forcing
    re-login.

    The new refresh token lives REFRESH_TOKEN_EXPIRE_DAYS (7 by default),
    but never past REFRESH_SESSION_MAX_DAYS (30 by default) from the login
    that started the session: from then on the refresh is refused (401, as
    for an expired token) and the user logs in again.
    """
    try:
        new_access_token, new_refresh_token = await rotate_refresh_token(
            refresh_in.refresh_token, db
        )
    except RefreshTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except TokenStateUnavailable as e:
        # Redis cannot say whether the token is good: not a 401, which would
        # make the client discard a refresh token that has not been spent.
        raise token_check_unavailable() from e

    return Token(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer"
    )

# One shared default: pydantic models are mutable, so a call in the signature is
# flagged (B008); logout only reads it.
_NO_LOGOUT_BODY = LogoutRequest()


@router.post(
    "/logout",
    response_model=Message,
    responses={503: {"description": "Tokens could not be revoked (Retry-After: 5), retry the request"}},
)
async def logout(
    token: BearerToken,
    current_user: CurrentUser,
    logout_in: LogoutRequest = _NO_LOGOUT_BODY,
):
    """Log out the current user: blacklist the access token in use and,
    if a refresh token is supplied, revoke its whole token family so it
    (and any refresh token rotated from it) can no longer be redeemed.
    Without a refresh token, every access and refresh token of the user
    issued before now is revoked: the user is signed out on every device.

    503 when that could not be recorded (Redis unavailable): the client
    must not be told it is logged out while its tokens still work."""
    try:
        await logout_tokens(token, logout_in.refresh_token)
    except RevocationUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Logout is temporarily unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    return Message(message="Logged out successfully")
