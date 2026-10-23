import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.v1.cart import merge_guest_cart_on_login
from app.core.clock import as_utc, utc_now
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import check_login_attempt, clear_login_failures
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenError,
    RevocationUnavailable,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_current_active_user,
    get_password_hash_async,
    invalidate_tokens_issued_before_now,
    issue_refresh_token,
    oauth2_scheme,
    rotate_refresh_token,
    verify_email_token,
    verify_password_async,
    verify_password_reset_token,
)
from app.core.security import (
    logout as logout_tokens,
)
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
    APIResponse,
    PaginationParams,
)
from app.schemas.order import OrderResponse
from app.schemas.user import (
    ConsentHistory,
    ConsentType,
    GDPRDelete,
    GDPRExport,
    GDPRExportData,
    LogoutRequest,
    PasswordReset,
    PasswordUpdate,
    RefreshTokenRequest,
    Token,
    UserCreate,
    UserResponse,
    UserUpdate,
    normalize_email,
)
from app.services.email import (
    send_account_exists_email,
    send_email_verification,
    send_gdpr_export_email,
    send_password_reset_email,
    send_welcome_email,
)
from app.services.redis import RedisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])
# oauth2_scheme is imported from app.core.security rather than redefined here
# with a second OAuth2PasswordBearer instance: a prior local definition used
# tokenUrl="users/login" (missing the /api/v1 prefix, unlike security.py's
# f"{settings.API_V1_STR}/users/login") - functionally harmless for token
# validation itself (that only inspects the bearer token, not the URL), but
# wrong for the Swagger "Authorize" button's login request and an
# unnecessary second source of truth for the same value.


def _user_by_email(db: Session, email: str):
    """The account for this email, in whatever case it was typed or stored.
    lower(email) on the column, not just the normalised input: rows stored
    before emails were normalised keep the spelling their owner typed. The
    comparison is what the unique index on lower(email) covers."""
    return db.query(User).filter(func.lower(User.email) == normalize_email(email)).first()



REGISTRATION_RECEIVED = (
    "Registration received. Check your inbox: we have sent you an email "
    "with what to do next."
)

@router.post(
    "/register",
    response_model=APIResponse[dict],
)
async def register_user(
    user_in: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
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
        db.commit()
        existing_email = None
        new_email, new_full_name, new_user_id = user.email, user.full_name, user.id
    except IntegrityError:
        # The email (in any case: see uq_users_email_lower) has an account.
        db.rollback()
        existing = _user_by_email(db, user_in.email)
        existing_email = existing.email if existing else None
        new_email = None
    # Nothing below touches `db`. Closing here returns the connection now
    # instead of leaving it to FastAPI's post-return dependency teardown,
    # which a client disconnect in the rest of the request can skip
    # entirely (fastapi.concurrency.contextmanager_in_threadpool has no
    # `finally` around its yield) - the exposure login()/reset_password()
    # close their sessions early for (see the long comment in login()).
    db.close()

    if background_tasks:
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

    return APIResponse.success_response({"message": REGISTRATION_RECEIVED})

@router.get(
    "/verify-email/{token}",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Invalid or expired verification token"},
        404: {"description": "User not found"},
    },
)
async def verify_email(
    token: str,
    db: Session = Depends(get_db),
):
    """Confirm a user's email address using a verification token."""

    email = verify_email_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token"
        )

    user = _user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if user.is_email_verified:
        return APIResponse.success_response({
            "message": "Email already verified"
        })

    user.is_email_verified = True
    user.email_verification_date = utc_now()
    db.commit()

    return APIResponse.success_response({
        "message": "Email verified successfully"
    })

@router.post(
    "/verify-email/resend",
    response_model=APIResponse[dict],
    responses={400: {"description": "Email already verified"}},
)
async def resend_verification_email(
    current_user: User = Depends(get_current_active_user),
    background_tasks: BackgroundTasks = None,
):
    """Resend the email verification link to the current user."""

    if current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already verified"
        )

    if background_tasks:
        verification_token = generate_email_verification_token(current_user.email)
        logger.info(f"Queuing verification email resend for user {current_user.id}")
        background_tasks.add_task(
            send_email_verification,
            current_user.email,
            verification_token
        )
        logger.info(f"Verification email resend task queued for user {current_user.id}")

    return APIResponse.success_response({
        "message": "Verification email sent"
    })

@router.post(
    "/login",
    response_model=APIResponse[Token],
    responses={
        401: {"description": "Incorrect email or password"},
        429: {"description": "Too many failed login attempts"},
        503: {"description": "Failed attempts cannot be counted right now, retry later"},
    },
)
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
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

    user = _user_by_email(db, form_data.username)
    user_id = user.id if user else None
    hashed_password = user.hashed_password if user else None
    # Read here with the others, off the row already loaded, because the
    # session is closed next.
    user_is_active = bool(user.is_active) if user else False

    # Release this request's DB connection back to the pool *before* the
    # CPU-bound bcrypt verification, instead of after. verify_password_async
    # offloads to a shared threadpool (see app/core/security.py) that also
    # carries every other concurrent request's bcrypt work plus FastAPI's own
    # threadpool dispatch of sync `Depends(get_db)` dependencies - under load
    # that queue backs up, so a connection held open across this await gets
    # held for however long the queue is, not just one hash's cost. Worse,
    # if the client disconnects while parked on this await, the connection
    # is stranded permanently: FastAPI's dependency cleanup (`session.close()`
    # in app.core.database.get_db) itself runs via another threadpool await,
    # and anyio delivers cancellation per-scope - once cancelled, that
    # cleanup checkpoint raises immediately instead of ever running
    # `session.close()`. Closing the session here, before the await, means
    # there is no open connection left for that cancellation to strand (this
    # was confirmed by instrumenting fastapi.concurrency.contextmanager_in_
    # threadpool directly: cancelling a task mid-await inside it never
    # invoked cm.__exit__ at all, even after waiting well past the awaited
    # call's completion). `db` itself is a plain SQLAlchemy Session, not a
    # connection - closing it here doesn't invalidate it, it just returns
    # the checked-out connection to the pool; the session transparently
    # checks out a fresh one on its next use below.
    db.close()

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
            detail="Incorrect email or password"
        )

    # A success wipes the email's failed-attempt count. After db.close()
    # above, so no database connection is held across this await.
    await clear_login_failures(form_data.username, request)

    db.query(User).filter(User.id == user_id).update(
        {User.last_login: utc_now()}
    )
    db.commit()

    logger.info(f"Generating tokens for user {user_id}")
    access_token = create_access_token(user_id)
    refresh_token = issue_refresh_token(user_id)
    logger.info(f"Tokens generated for user {user_id}")

    # The cart the visitor filled before logging in becomes part of the
    # account's cart. Last, and Redis only: the password is verified, the
    # last_login update is committed (so the session holds no connection
    # here, as after the db.close() above) and the tokens exist, and
    # nothing in it can raise - the login succeeds whatever becomes of the
    # cart. Only an active account gets this far (see the refusal above).
    merge_guest_cart_on_login(request, response, user_id, redis)

    return APIResponse.success_response(Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer"
    ))

@router.post(
    "/refresh",
    response_model=APIResponse[Token],
    responses={401: {"description": "Invalid, expired, or already-used refresh token"}},
)
async def refresh_access_token(
    refresh_in: RefreshTokenRequest,
    db: Session = Depends(get_db),
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
        )

    return APIResponse.success_response(Token(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer"
    ))

@router.post(
    "/logout",
    response_model=APIResponse[dict],
    responses={503: {"description": "Tokens could not be revoked, retry the request"}},
)
async def logout(
    logout_in: LogoutRequest = LogoutRequest(),
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_active_user),
):
    """Log out the current user: blacklist the access token in use and,
    if a refresh token is supplied, revoke its whole token family so it
    (and any refresh token rotated from it) can no longer be redeemed.
    Without a refresh token, every access and refresh token of the user
    issued before now is revoked: the user is signed out on every device.

    503 when that could not be recorded (Redis unavailable): the client
    must not be told it is logged out while its tokens still work."""
    try:
        logout_tokens(token, logout_in.refresh_token)
    except RevocationUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Logout is temporarily unavailable, please retry"
        )
    return APIResponse.success_response({"message": "Logged out successfully"})

# Where each consent type is stored on User: (flag, date). Spelled out
# rather than derived from the type's name, which is what this route used
# to do - f"{type}_consent" - and "privacy_policy_consent" is no column
# (the column is privacy_policy_accepted), nor is "cookies_consent": the
# flag was set on a plain Python attribute and lost, while the history
# recorded the change and the route answered 200. Cookie consent has no
# column at all; it is the cookie banner's business, and refused here.
_CONSENT_COLUMNS = {
    ConsentType.GDPR: ("gdpr_consent", "gdpr_consent_date"),
    ConsentType.MARKETING: ("marketing_consent", "marketing_consent_date"),
    ConsentType.PRIVACY_POLICY: ("privacy_policy_accepted", "privacy_policy_accepted_date"),
}

@router.post(
    "/consent",
    response_model=APIResponse[UserResponse],
    responses={
        400: {
            "description": "GDPR consent cannot be revoked via this endpoint; "
                           "cookie consent is not stored on the account"
        },
    },
)
async def update_consent(
    consent_type: ConsentType,
    granted: bool,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Update the current user's consent for a given consent type."""

    if consent_type == ConsentType.GDPR and not granted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GDPR consent cannot be revoked. Use data deletion instead."
        )
    columns = _CONSENT_COLUMNS.get(consent_type)
    if columns is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cookie consent is not stored on the account. Use the cookie banner."
        )
    flag_column, date_column = columns

    # current_user comes from get_current_active_user -> get_current_user
    # (app/core/security.py), which loads the row through an AsyncSession
    # from app.core.database.get_async_db - not through this route's `db`
    # (app.core.database.get_db, a sync Session). Different dependency
    # callables, so FastAPI's per-request cache gives each its own session,
    # and what the route receives is a detached snapshot of the row: it
    # belongs to no session this route can commit. Mutating current_user and
    # calling db.commit() was a silent no-op - the change was never written,
    # so consent updates never persisted despite the endpoint returning 200
    # with the (in-memory-only) updated value. Re-querying through `db`
    # attaches the row to the session this route actually commits.
    user = db.query(User).filter(User.id == current_user.id).first()

    setattr(user, flag_column, granted)
    setattr(user, date_column, utc_now())

    # Add to consent history (Art. 7(1): controller must be able to
    # demonstrate consent was given). Shape matches
    # GDPRService.update_user_consent so the audit trail is consistent
    # regardless of which endpoint (this one, or POST /legal/consent) was used.
    # Reassigning (not .append()-ing in place) is required: consent_history is
    # a plain JSON column, and SQLAlchemy does not detect in-place mutations
    # of mutable values on JSON columns, so an in-place append here silently
    # fails to persist.
    user.consent_history = user.consent_history + [{
        "type": consent_type.value,
        "granted": granted,
        "timestamp": utc_now().isoformat(),
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }]

    db.commit()
    db.refresh(user)
    return APIResponse.success_response(UserResponse.model_validate(user))

@router.get(
    "/data/export",
    response_model=APIResponse[GDPRExportData],
    responses={400: {"description": "Too much data for an inline export (GDPR_EXPORT_MAX_RECORDS)"}},
)
async def export_user_data(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """Export the current user's personal data, consents, addresses, and orders (GDPR)."""

    consents = [
        ConsentHistory(
            type=entry["type"],
            granted=entry["granted"],
            timestamp=as_utc(datetime.fromisoformat(entry["timestamp"])),
            ip_address=entry.get("ip_address"),
            user_agent=entry.get("user_agent"),
        )
        for entry in current_user.consent_history
    ]

    now = utc_now()
    export_metadata = GDPRExport(
        request_id=f"export_{current_user.id}_{now.timestamp()}",
        request_date=now,
        expires_at=now + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS),
        status="completed",
    )
    # Loaded here, eagerly, through this route's own session - not read off
    # current_user.addresses / current_user.orders. Those lazy relationships
    # cost one query per order, per item, per product and per variant when
    # OrderResponse serialized them: 165 queries for one export of an account
    # with a handful of orders (measured by tracing, scripts/loadtest/
    # FINDINGS.md). It also depended on current_user still being attached to
    # a live session, which is the auth dependency's business, not this
    # route's.
    #
    # Counted first, in one query, and refused past GDPR_EXPORT_MAX_RECORDS:
    # the export is assembled in this worker's memory and sent as one
    # response, so its size must have a ceiling. Past it the holder is told
    # where to get the data instead - the request itself (Art. 15) still has
    # to be answered, just not inline.
    records = db.query(
        select(func.count()).select_from(Address)
        .where(Address.user_id == current_user.id).scalar_subquery()
        + select(func.count()).select_from(Order)
        .where(Order.user_id == current_user.id).scalar_subquery()
        + select(func.count()).select_from(OrderItem)
        .join(Order, OrderItem.order_id == Order.id)
        .where(Order.user_id == current_user.id).scalar_subquery()
    ).scalar()
    if records > settings.GDPR_EXPORT_MAX_RECORDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": (
                    "Your data is too large for an export here. Contact "
                    f"{settings.DPO_EMAIL} and it will be sent to you."
                ),
                "records": records,
                "limit": settings.GDPR_EXPORT_MAX_RECORDS,
            },
        )
    addresses = db.query(Address).filter(Address.user_id == current_user.id).all()
    orders = (
        db.query(Order)
        .options(
            joinedload(Order.shipping_address),
            selectinload(Order.items).joinedload(OrderItem.product),
            selectinload(Order.items).joinedload(OrderItem.variant),
        )
        .filter(Order.user_id == current_user.id)
        .all()
    )
    export = GDPRExportData(
        personal_data=current_user,
        consents=consents,
        addresses=addresses,
        orders=orders,
        export_metadata=export_metadata,
    )


    if background_tasks:
        logger.info(f"Queuing GDPR data export email for user {current_user.id}")
        background_tasks.add_task(
            send_gdpr_export_email,
            current_user.email,
            export_metadata
        )
        logger.info(f"GDPR data export email task queued for user {current_user.id}")

    return APIResponse.success_response(export)

@router.post(
    "/data/delete",
    response_model=APIResponse[dict],
    responses={401: {"description": "Incorrect password"}},
)
async def delete_user_data(
    deletion: GDPRDelete,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Request deletion of the current user's account and data (GDPR)."""

    if not await verify_password_async(deletion.password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password"
        )

    # See the identical note in update_consent above: current_user is a
    # detached snapshot loaded through get_async_db (via
    # get_current_active_user), not attached to this route's `db`
    # (app.core.database.get_db), so mutating current_user directly and
    # committing `db` silently discarded the deactivation/deletion flags -
    # the account was never actually deactivated. Re-query through `db`.
    # (The former `current_user.deletion_reason = deletion.reason` line is
    # also dropped: User has no deletion_reason column, so it only ever set
    # a transient, non-persisted Python attribute.)
    user = db.query(User).filter(User.id == current_user.id).first()
    user.is_active = False
    user.data_deletion_requested = True
    user.data_deletion_date = utc_now()
    db.commit()

    logger.info(f"Scheduling hard delete for user {user.id} after 1 day")

    return APIResponse.success_response({
        "message": "Account will be permanently deleted within 24 hours",
        "deletion_date": user.data_deletion_date
    })

@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_data(
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve the current authenticated user's profile."""

    return APIResponse.success_response(UserResponse.model_validate(current_user))

@router.put(
    "/me",
    response_model=APIResponse[UserResponse],
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        500: {"description": "Failed to update profile"},
    },
)
async def update_current_user(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
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

    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    try:

        for field, new_value in update_data.items():
            setattr(user, field, new_value)

        db.commit()
        logger.info(
            f"Profile updated successfully for user {user.id}. "
            f"Updated fields: {list(update_data.keys())}"
        )


        db.refresh(user)
        return APIResponse.success_response(UserResponse.model_validate(user))

    except Exception as e:
        db.rollback()
        # The exception's type only: its message can quote the values.
        logger.error(f"Failed to update profile for user {current_user.id}: {type(e).__name__}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile"
        )

@router.get("/addresses", response_model=APIResponse[AddressListResponse])
async def list_addresses(
    pagination: PaginationParams = Depends(),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """List the current user's active addresses, paginated."""

    total = db.query(Address).filter(
        Address.user_id == current_user.id,
        Address.is_active
    ).count()

    addresses = (
        db.query(Address)
        .filter(
            Address.user_id == current_user.id,
            Address.is_active
        )
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
        .all()
    )

    return APIResponse.success_response(AddressListResponse(
        items=addresses,
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > (pagination.page * pagination.size)
    ))

@router.post(
    "/addresses",
    response_model=APIResponse[AddressResponse],
    responses={
        409: {"description": "This address already exists for the current user"},
        422: {"description": "Invalid address data"},
    },
)
async def create_address(
    address_in: AddressCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Add a new address for the current user."""

    try:

        active_addresses = db.query(Address).filter(
            Address.user_id == current_user.id,
            Address.is_active
        ).all()


        should_be_default = address_in.is_default or not active_addresses
        if should_be_default:

            existing_default = db.query(Address).filter(
                Address.user_id == current_user.id,
                Address.is_active,
                Address.is_default
            ).first()
            if existing_default:
                existing_default.is_default = False


        address = Address(
            user_id=current_user.id,
            is_default=should_be_default,
            **address_in.model_dump(exclude={'is_default'})
        )

        db.add(address)
        db.commit()
        db.refresh(address)

        logger.info(f"Created address with ID: {address.id} for user {current_user.id}")


        created = db.get(Address, address.id)
        logger.info(f"Verified address exists: {created and created.id} for user {current_user.id}")

        return APIResponse.success_response(AddressResponse.model_validate(address))
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Invalid address data",
                "errors": e.errors()
            }
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This address already exists for the current user"
        )

@router.put(
    "/addresses/{address_id}",
    response_model=APIResponse[AddressResponse],
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
async def update_address(
    address_id: int,
    address_in: AddressUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Update fields on one of the current user's addresses."""

    address = db.query(Address).filter(
        Address.id == address_id,
        Address.user_id == current_user.id
    ).first()

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
        existing_default = db.query(Address).filter(
            Address.user_id == current_user.id,
            Address.is_active,
            Address.is_default,
            Address.id != address_id
        ).first()
        if existing_default:
            existing_default.is_default = False


    for field, value in address_in.model_dump(exclude_unset=True).items():
        setattr(address, field, value)

    db.commit()
    db.refresh(address)

    return APIResponse.success_response(AddressResponse.model_validate(address))

@router.delete(
    "/addresses/{address_id}",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
async def delete_address(
    address_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Deactivate one of the current user's addresses."""

    address = db.query(Address).filter(
        Address.id == address_id,
        Address.user_id == current_user.id
    ).first()

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
        new_default = db.query(Address).filter(
            Address.user_id == current_user.id,
            Address.is_active,
            Address.id != address_id
        ).order_by(Address.created_at.desc()).first()
        if new_default:
            new_default.is_default = True

    db.commit()

    return APIResponse.success_response({
        "message": "Address deleted successfully"
    })

@router.post(
    "/addresses/default",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Address is not active"},
        404: {"description": "Address not found"},
    },
)
async def set_default_address(
    default_address: SetDefaultAddress,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Mark one of the current user's addresses as the default shipping address."""

    address = db.query(Address).filter(
        Address.id == default_address.address_id,
        Address.user_id == current_user.id
    ).first()

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


    db.query(Address).filter(
        Address.user_id == current_user.id,
        Address.is_active,
        Address.id != address.id
    ).update({Address.is_default: False})

    db.commit()

    return APIResponse.success_response({
        "message": "Default address updated successfully"
    })

@router.get("/orders", response_model=APIResponse[List[OrderResponse]])
async def get_user_orders(
    pagination: PaginationParams = Depends(),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """List the current user's orders, most recent first, paginated."""

    # Eager-load shipping_address (many-to-one) and items->product
    # (one-to-many): OrderResponse serializes both for every order in the
    # page, so without this each row triggers its own lazy-load queries.
    orders = (
        db.query(Order)
        .options(
            joinedload(Order.shipping_address),
            selectinload(Order.items).joinedload(OrderItem.product),
        )
        .filter(Order.user_id == current_user.id)
        .order_by(Order.created_at.desc())
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
        .all()
    )
    return APIResponse.success_response([
        OrderResponse.model_validate(order) for order in orders
    ])

def _invalid_reset_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired reset token"
    )

@router.post("/password/reset", response_model=APIResponse[dict])
async def request_password_reset(
    reset_request: PasswordReset,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """Request a password reset email; always succeeds to avoid leaking registered emails."""

    user = _user_by_email(db, reset_request.email)
    if user:
        token = generate_password_reset_token(user.email, user.hashed_password)
        if background_tasks:
            logger.info(f"Queuing password reset email for user {user.id}")
            background_tasks.add_task(
                send_password_reset_email,
                user.email,
                token
            )
            logger.info(f"Password reset email task queued for user {user.id}")


    return APIResponse.success_response({
        "message": "If the email exists, a password reset link will be sent"
    })

@router.get(
    "/password/reset/{token}",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Invalid or expired reset token"},
        404: {"description": "User not found"},
    },
)
async def validate_reset_token(
    token: str,
    db: Session = Depends(get_db),
):
    """Check whether a password reset token is still valid."""

    claims = verify_password_reset_token(token)
    if not claims:
        raise _invalid_reset_token()
    email = claims.email

    user = _user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    if not claims.matches(user.hashed_password):
        # Already used, or the password has changed since it was issued.
        raise _invalid_reset_token()

    # No email in the answer: the token travels in a URL, and whoever sees
    # it must not learn whose account it opens just by checking it.
    return APIResponse.success_response({
        "message": "Token is valid"
    })

@router.post(
    "/password/reset/{token}",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Invalid or expired reset token"},
        404: {"description": "User not found"},
        422: {"description": "Invalid password format"},
        503: {"description": "Old tokens could not be revoked, nothing changed; retry"},
    },
)
async def reset_password(
    token: str,
    new_password: PasswordUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reset a user's password using a valid reset token."""

    try:
        claims = verify_password_reset_token(token)
        if not claims:
            raise _invalid_reset_token()
        email = claims.email

        user = _user_by_email(db, email)
        user_id = user.id if user else None
        old_hashed_password = user.hashed_password if user else None

        # Same reasoning as login(): release the connection before the
        # CPU-bound bcrypt hash, not after, so neither queueing delay nor a
        # mid-await client disconnect can hold/strand it. See the long
        # comment in login() for the full mechanism. Before the refusals
        # below as well: a refused request would otherwise keep its
        # connection until dependency teardown gets a thread, and under a
        # burst of resets that thread is queued behind every bcrypt hash.
        db.close()

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

        new_hashed_password = await get_password_hash_async(new_password.new_password)

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
        if not invalidate_tokens_issued_before_now(user_id):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Password reset is temporarily unavailable, please retry"
            )

        # Only if the password is still the one the token was checked
        # against: two requests presenting the same token both pass that
        # check above, and only the first to get here may use it.
        updated = db.query(User).filter(
            User.id == user_id, User.hashed_password == old_hashed_password
        ).update({User.hashed_password: new_hashed_password}, synchronize_session=False)
        db.commit()
        if updated != 1:
            raise _invalid_reset_token()

        # The way out of a login refused for too many failed attempts: the
        # new password starts with a clean record - the email's own count
        # and its count at this address (other addresses' counts are their
        # failures, and lock only them). After the commit, so no database
        # connection is held across it.
        await clear_login_failures(email, request)

        return APIResponse.success_response({
            "message": "Password has been reset successfully"
        })
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Invalid password format",
                "errors": e.errors()
            }
        )
