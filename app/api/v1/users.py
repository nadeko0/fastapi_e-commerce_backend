import logging
from typing import Annotated

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

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.cart import merge_guest_cart_on_login_async
from app.core.clock import utc_now
from app.core.dependencies import BearerToken, Cache, CurrentUser, DbSession
from app.core.errors import Problem, ProblemType, sanitized_errors
from app.core.openapi import DocumentedRoute
from app.core.passwords import BLOCKED_MESSAGE, is_blocked_password
from app.core.rate_limit import check_login_attempt, clear_login_failures
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenError,
    RevocationUnavailable,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_password_hash_async,
    invalidate_tokens_issued_before_now,
    issue_refresh_token,
    rotate_refresh_token,
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

logger = logging.getLogger(__name__)

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
    result = await db.execute(
        select(User).where(func.lower(User.email) == normalize_email(email)).limit(1)
    )
    return result.scalars().first()



REGISTRATION_RECEIVED = (
    "Registration received. Check your inbox: we have sent you an email "
    "with what to do next."
)

@router.post(
    "/register",
    response_model=Message,
)
async def register_user(
    user_in: UserCreate,
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Register a new user account and queue a welcome/verification email.

    Answers exactly the same whether or not the email already has an
    account, so that registration cannot be used to find out which emails
    are registered (login and password reset already take care not to).
    For an existing account nothing is created or changed; its owner gets
    a "you already have an account" email instead of the welcome one."""
    now = utc_now()
    client_ip = request.client.host if request.client else None
    client_ua = request.headers.get("user-agent")
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
    body."""

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
    responses={400: {"description": "Email already verified"}},
)
async def resend_verification_email(
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """Resend the email verification link to the current user."""

    if current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
        401: {"description": "Incorrect email or password"},
        429: {"description": "Too many failed login attempts"},
        503: {"description": "Failed attempts cannot be counted right now, retry later"},
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
    refresh_token = await issue_refresh_token(user_id)
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
    responses={401: {"description": "Invalid, expired, or already-used refresh token"}},
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
    responses={503: {"description": "Tokens could not be revoked, retry the request"}},
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
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("UserUpdate"),
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        500: {"description": "Failed to update profile"},
    },
)
@router.put(
    "/me",
    response_model=UserResponse,
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        500: {"description": "Failed to update profile"},
    },
)
async def update_current_user(
    user_in: UserUpdate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on the current authenticated user's profile."""

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

    try:

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
AddressListParams = sortable_pagination("AddressListParams", tuple(ADDRESS_SORT_COLUMNS))
ORDER_SORT_COLUMNS = {"id": Order.id, "created_at": Order.created_at, "total_amount": Order.total_amount}
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

@router.post(
    "/addresses",
    response_model=AddressResponse,
    responses={
        409: {"description": "This address already exists for the current user"},
        422: {"description": "Invalid address data"},
    },
)
async def create_address(
    address_in: AddressCreate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Add a new address for the current user."""

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
    "/addresses/{address_id}",
    response_model=AddressResponse,
    summary="Patch Address",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("AddressUpdate"),
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
@router.put(
    "/addresses/{address_id}",
    response_model=AddressResponse,
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
async def update_address(
    address_id: Int32,
    address_in: AddressUpdate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on one of the current user's addresses."""

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
            status_code=status.HTTP_400_BAD_REQUEST,
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
    "/addresses/{address_id}",
    response_model=Message,
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
async def delete_address(
    address_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """Deactivate one of the current user's addresses."""

    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info(f"Processing deletion request for address {address_id} from user {current_user.id}")

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )

    if not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Address {address_id} is not active"
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

    return Message(message="Address deleted successfully")

@router.post(
    "/addresses/default",
    response_model=Message,
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
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
            status_code=status.HTTP_400_BAD_REQUEST,
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
    """List the current user's orders, most recent first, paginated."""

    total = await db.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == current_user.id)
    )

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
    """Request a password reset email; always succeeds to avoid leaking registered emails."""

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
    It changes nothing."""

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
        422: {"description": "Invalid password format"},
        503: {"description": "Old tokens could not be revoked, nothing changed; retry"},
    },
)
async def reset_password(
    payload: PasswordResetConfirm,
    request: Request,
    db: DbSession,
):
    """Reset a user's password using a valid reset token, both sent in the body."""

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

    if user_id is None:
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
    updated = result.rowcount
    await db.commit()
    if updated != 1:
        raise _invalid_reset_token()

    # The way out of a login refused for too many failed attempts: the
    # new password starts with a clean record - the email's own count
    # and its count at this address (other addresses' counts are their
    # failures, and lock only them). After the commit, so no database
    # connection is held across it.
    await clear_login_failures(email, request)

    return Message(message="Password has been reset successfully")
