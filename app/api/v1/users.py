import contextlib
import logging
from typing import TYPE_CHECKING, Annotated

from email_validator import EmailNotValidError, validate_email
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from starlette.convertors import Convertor, register_url_convertor

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.cart import merge_guest_cart_on_login_async
from app.core.clock import utc_now
from app.core.dependencies import BearerToken, Cache, CurrentUser, DbSession
from app.core.errors import Problem, ProblemType, sanitized_errors
from app.core.location import set_location
from app.core.openapi import CLEAR_GUEST_CART_COOKIE_RESPONSE, LOCATION_HEADER, DocumentedRoute
from app.core.passwords import BLOCKED_MESSAGE, is_blocked_password
from app.core.rate_limit import (
    check_consent_change,
    check_login_attempt,
    clear_login_failures,
    login_unavailable,
)
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenError,
    RevocationUnavailable,
    async_redis_service,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_password_hash_async,
    invalidate_tokens_issued_before_now,
    issue_refresh_token,
    rotate_refresh_token,
    token_check_unavailable,
    verify_email_token,
    verify_password_async,
    verify_password_reset_token,
)
from app.core.security import (
    logout as logout_tokens,
)
from app.core.sorting import order_clauses
from app.models.address import Address
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.user import User
from app.schemas.address import (
    AddressCreate,
    AddressListResponse,
    AddressResponse,
    AddressUpdate,
    SetDefaultAddress,
)
from app.schemas.common import (
    Int32,
    Message,
    PaginationParams,
    sortable_pagination,
)
from app.schemas.order import OrderListResponse, OrderResponse
from app.schemas.user import (
    ConsentType,
    LogoutRequest,
    PasswordReset,
    PasswordResetConfirm,
    RefreshTokenRequest,
    Token,
    TokenBody,
    UserCreate,
    UserResponse,
    UserUpdate,
    normalize_email,
)
from app.services.email import (
    send_account_exists_email,
    send_email_verification,
    send_password_reset_email,
    send_welcome_email,
)
from app.services.gdpr import consent_user_agent
from app.services.redis_async import TokenStateUnavailable

logger = logging.getLogger(__name__)

class SignedIntConvertor(Convertor[int]):
    """Starlette's `int` matches digits only, so a negative id would no longer
    reach the route and answer 422 (the Int32 bound) but 404. This one still
    refuses what is not a number, which is what lets the literal
    /addresses/default fall through to its own route."""

    regex = "-?[0-9]+"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(int(value))


register_url_convertor("sint", SignedIntConvertor())

router = APIRouter(prefix="/users", tags=["users"], route_class=DocumentedRoute)
# oauth2_scheme is imported from app.core.security rather than redefined here
# with a second OAuth2PasswordBearer instance: a prior local definition used
# tokenUrl="users/login" (missing the /api/v1 prefix, unlike security.py's
# f"{settings.API_V1_STR}/users/login") - functionally harmless for token
# validation itself (that only inspects the bearer token, not the URL), but
# wrong for the Swagger "Authorize" button's login request and an
# unnecessary second source of truth for the same value.


async def _user_by_email(db: AsyncSession, email: str):
    """The account for this email, in whatever case it was typed or stored.
    lower(email) on the column, not just the normalised input: rows stored
    before emails were normalised keep the spelling their owner typed. The
    comparison is what the unique index on lower(email) covers."""
    canonical = normalize_email(email)
    # The spelling registration stores (EmailStr: IDNA / UTS #46 domain). An
    # address that is not valid is simply not found: a plain 401, like any other.
    with contextlib.suppress(EmailNotValidError):
        canonical = validate_email(canonical, check_deliverability=False).normalized
    result = await db.execute(
        select(User).where(func.lower(User.email) == canonical).limit(1)
    )
    return result.scalars().first()



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
        logger.info(f"Queuing welcome email for user {new_user_id}")
        background_tasks.add_task(
            send_welcome_email,
            new_email,
            new_full_name,
            verification_token
        )
        logger.info(f"Welcome email task queued for user {new_user_id}")
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
    logger.info(f"Queuing verification email resend for user {current_user.id}")
    background_tasks.add_task(
        send_email_verification,
        current_user.email,
        verification_token
    )
    logger.info(f"Verification email resend task queued for user {current_user.id}")

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

    logger.info(f"Generating tokens for user {user_id}")
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
    logger.info(f"Tokens generated for user {user_id}")

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
                             "token's family: log in again)"},
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

@router.get("/me", response_model=UserResponse)
async def get_current_user_data(
    current_user: CurrentUser,
):
    """Retrieve the current authenticated user's profile."""

    return UserResponse.model_validate(current_user)

# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Patch Current User",
    description=(
        PATCH_DESCRIPTION
        + "\n\nAt least one field is required (else 400). `marketing_consent` cannot be null. A change of "
        "`marketing_consent` is recorded in the consent history and counts against RATE_LIMIT_CONSENT "
        "(10/hour by default, per user, shared with POST /legal/consent): over the limit the answer is 429 "
        "with Retry-After. Order placement needs a full name and a phone number."
    ),
    openapi_extra=merge_patch_body("UserUpdate"),
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        429: {"description": "Too many requests, or too many consent changes: a change of marketing_consent is "
                             "limited per user by RATE_LIMIT_CONSENT (10/hour by default), shared with "
                             "POST /legal/consent; sending the value it already has is not counted"},
        500: {"description": "Failed to update profile"},
    },
)
@router.put(
    "/me",
    response_model=UserResponse,
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        429: {"description": "Too many requests, or too many consent changes: a change of marketing_consent is "
                             "limited per user by RATE_LIMIT_CONSENT (10/hour by default), shared with "
                             "POST /legal/consent; sending the value it already has is not counted"},
        500: {"description": "Failed to update profile"},
    },
)
async def update_current_user(
    user_in: UserUpdate,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on the current authenticated user's profile.

    At least one field is required (else 400). `full_name` and `phone` may be
    cleared with null; `marketing_consent` cannot be null. A change of
    `marketing_consent` is recorded in the consent history and counts
    against RATE_LIMIT_CONSENT (10/hour by default, per user, shared with
    POST /legal/consent): over the limit the answer is 429 with Retry-After.
    Order placement needs a full name and a phone number."""

    # Field names only, never their values: names and phone numbers are
    # personal data, and the logs are kept apart from the account and not
    # scrubbed when it is erased.
    logger.info(f"Updating profile for user {current_user.id}")

    update_data = user_in.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )

    logger.info(f"Fields to update: {list(update_data.keys())}")

    user = await db.get(User, current_user.id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Outside the try below, which turns every exception into a 500.
    new_consent = update_data.get("marketing_consent")
    if new_consent is not None and new_consent != user.marketing_consent:
        await check_consent_change(user.id)

    try:
        consent = update_data.pop("marketing_consent", None)
        if consent is not None and consent != user.marketing_consent:
            # Art. 7(1): a consent change must be demonstrable. Same entry
            # shape as register_user and GDPRService.update_user_consent.
            now = utc_now()
            user.consent_history = (user.consent_history or []) + [{
                "type": "marketing",
                "granted": consent,
                "timestamp": now.isoformat(),
                "ip_address": request.client.host if request.client else None,
                "user_agent": consent_user_agent(request),
            }]
            user.marketing_consent = consent
            user.marketing_consent_date = now
        for field, new_value in update_data.items():
            setattr(user, field, new_value)

        await db.commit()
        logger.info(
            f"Profile updated successfully for user {user.id}. "
            f"Updated fields: {list(update_data.keys())}"
        )


        await db.refresh(user)
        return UserResponse.model_validate(user)

    except Exception as e:
        await db.rollback()
        # The exception's type only: its message can quote the values.
        logger.error(f"Failed to update profile for user {current_user.id}: {type(e).__name__}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile"
        ) from e

ADDRESS_SORT_COLUMNS = {"id": Address.id, "created_at": Address.created_at}
ORDER_SORT_COLUMNS = {"id": Order.id, "created_at": Order.created_at, "total_amount": Order.total_amount}
if TYPE_CHECKING:
    AddressListParams = PaginationParams
    UserOrderListParams = PaginationParams
else:
    AddressListParams = sortable_pagination("AddressListParams", tuple(ADDRESS_SORT_COLUMNS))
    UserOrderListParams = sortable_pagination("UserOrderListParams", tuple(ORDER_SORT_COLUMNS))


@router.get("/addresses", response_model=AddressListResponse)
async def list_addresses(
    pagination: Annotated[AddressListParams, Query()],
    current_user: CurrentUser,
    db: DbSession,
):
    """List the current user's active addresses, paginated."""

    total = await db.scalar(
        select(func.count()).select_from(Address).where(
            Address.user_id == current_user.id,
            Address.is_active
        )
    )
    assert total is not None, "SELECT count(*) always returns an integer"

    addresses = (await db.execute(
        select(Address)
        .where(
            Address.user_id == current_user.id,
            Address.is_active
        )
        .order_by(*order_clauses(
            pagination.sort_by, pagination.sort_order, ADDRESS_SORT_COLUMNS, Address.id, [Address.id]
        ))
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
    )).scalars().all()

    return AddressListResponse(
        items=addresses,
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > (pagination.page * pagination.size)
    )

@router.get(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    responses={404: {"description": "Address not found, deleted, or not the current user's"}},
)
async def get_address(
    address_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """One of the current user's active addresses.

    The address of another user, a deleted one and a missing one are the
    same 404, so the id says nothing about who owns it."""

    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id,
            Address.is_active
        ).limit(1)
    )).scalars().first()

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )

    return AddressResponse.model_validate(address)

@router.post(
    "/addresses",
    response_model=AddressResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Address created; Location is its URL", "headers": LOCATION_HEADER},
        409: {"description": "This address already exists for the current user (same street, city, state, "
                             "postal code and country as an active address)"},
        422: {"description": "Invalid address data"},
    },
)
async def create_address(
    request: Request,
    response: Response,
    address_in: AddressCreate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Add a new address for the current user.

    The first address, and one sent with `is_default` true, becomes the
    default; the previous default is unset. Whitespace in `postal_code` is
    removed and `country` is normalised (a two-letter code is upper-cased).
    There is no limit on the number of addresses."""

    try:

        active_addresses = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active
            )
        )).scalars().all()


        should_be_default = address_in.is_default or not active_addresses
        if should_be_default:

            existing_default = (await db.execute(
                select(Address).where(
                    Address.user_id == current_user.id,
                    Address.is_active,
                    Address.is_default
                ).limit(1)
            )).scalars().first()
            if existing_default:
                existing_default.is_default = False


        address = Address(
            user_id=current_user.id,
            is_default=should_be_default,
            **address_in.model_dump(exclude={'is_default'})
        )

        db.add(address)
        await db.commit()
        # address_type has a server default the INSERT does not read back.
        await db.refresh(address)

        logger.info(f"Created address with ID: {address.id} for user {current_user.id}")


        created = await db.get(Address, address.id)
        logger.info(f"Verified address exists: {created and created.id} for user {current_user.id}")

        set_location(request, response, "get_address", address_id=address.id)
        return AddressResponse.model_validate(address)
    except ValidationError as e:
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.VALIDATION_ERROR,
            detail="Invalid address data",
            errors=sanitized_errors(e.errors()),
        ) from None
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This address already exists for the current user"
        ) from e

# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    summary="Patch Address",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("AddressUpdate"),
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": "Address is not active (it was deleted)"},
    },
)
@router.put(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": "Address is not active (it was deleted)"},
    },
)
async def update_address(
    address_id: Int32,
    address_in: AddressUpdate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on one of the current user's addresses. Sending
    `is_default` true makes it the default and unsets the previous one. An
    order keeps the address it was placed with: a later change does not
    alter it."""

    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info(f"Found address {address_id} for update request from user {current_user.id}")

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )

    if not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {address_id} is not active"
        )




    if address_in.is_default:
        existing_default = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active,
                Address.is_default,
                Address.id != address_id
            ).limit(1)
        )).scalars().first()
        if existing_default:
            existing_default.is_default = False


    for field, value in address_in.model_dump(exclude_unset=True).items():
        setattr(address, field, value)

    await db.commit()
    await db.refresh(address)

    return AddressResponse.model_validate(address)

@router.delete(
    "/addresses/{address_id:sint}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"description": "Address not found, or already deleted"}},
)
async def delete_address(
    address_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """Deactivate one of the current user's addresses.

    A deactivated address is gone for the client, so deleting it again is a
    404 like any other address that does not exist. Orders already placed
    with it keep it. If it was the default, the most recently created
    remaining address becomes the default."""

    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info(f"Processing deletion request for address {address_id} from user {current_user.id}")

    if not address or not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )


    address.is_active = False


    if address.is_default:
        new_default = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active,
                Address.id != address_id
            ).order_by(Address.created_at.desc()).limit(1)
        )).scalars().first()
        if new_default:
            new_default.is_default = True

    await db.commit()

@router.post(
    "/addresses/default",
    response_model=Message,
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": "Address is not active (it was deleted)"},
    },
)
async def set_default_address(
    default_address: SetDefaultAddress,
    current_user: CurrentUser,
    db: DbSession,
):
    """Mark one of the current user's addresses as the default shipping address."""

    address = (await db.execute(
        select(Address).where(
            Address.id == default_address.address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info(f"Processing set default request for address {default_address.address_id} from user {current_user.id}")

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {default_address.address_id} not found"
        )

    if not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {default_address.address_id} is not active"
        )


    address.is_default = True


    await db.execute(
        update(Address)
        .where(
            Address.user_id == current_user.id,
            Address.is_active,
            Address.id != address.id
        )
        .values(is_default=False)
        .execution_options(synchronize_session=False)
    )

    await db.commit()

    return Message(message="Default address updated successfully")

@router.get("/orders", response_model=OrderListResponse)
async def get_user_orders(
    pagination: Annotated[UserOrderListParams, Query()],
    current_user: CurrentUser,
    db: DbSession,
):
    """List the current user's orders, most recent first, paginated (`page`
    from 1, `size` 1 to 100, 20 by default). `sort_by` is one of id,
    created_at, total_amount, with `sort_order` asc (the default when `sort_by`
    is given) or desc. An unpaid order shows status `new` until it is paid,
    cancelled, or expired UNPAID_ORDER_EXPIRE_MINUTES (30 by default) after
    it was placed."""

    total = await db.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == current_user.id)
    )
    assert total is not None, "SELECT count(*) always returns an integer"

    # Eager-load shipping_address (many-to-one) and items->product/variant
    # (one-to-many): OrderResponse serializes them for every order in the
    # page (variant_sku reads item.variant), and an AsyncSession cannot
    # lazy-load them while the response is built.
    orders = (await db.execute(
        select(Order)
        .options(
            joinedload(Order.shipping_address),
            selectinload(Order.items).joinedload(OrderItem.product),
            selectinload(Order.items).joinedload(OrderItem.variant),
        )
        .where(Order.user_id == current_user.id)
        .order_by(*order_clauses(
            pagination.sort_by, pagination.sort_order, ORDER_SORT_COLUMNS, Order.id,
            [Order.created_at.desc(), Order.id.desc()],
        ))
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
    )).scalars().all()
    return OrderListResponse(
        items=[OrderResponse.model_validate(order) for order in orders],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )

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
        logger.info(f"Queuing password reset email for user {user.id}")
        background_tasks.add_task(
            send_password_reset_email,
            user.email,
            token
        )
        logger.info(f"Password reset email task queued for user {user.id}")


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
        logger.error(f"Password reset of user {user_id}: second watermark not recorded")
    if not await async_redis_service.revoke_user_refresh_families(user_id):
        logger.error(f"Password reset of user {user_id}: refresh families not revoked")

    # The way out of a login refused for too many failed attempts: the
    # new password starts with a clean record - the email's own count
    # and its count at this address (other addresses' counts are their
    # failures, and lock only them). After the commit, so no database
    # connection is held across it.
    await clear_login_failures(email, request)

    return Message(message="Password has been reset successfully")
