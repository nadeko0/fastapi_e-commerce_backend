import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenError,
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
)
from app.services.email import (
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



@router.post(
    "/register",
    response_model=APIResponse[UserResponse],
    responses={409: {"description": "Email already registered"}},
)
async def register_user(
    user_in: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """Register a new user account and queue a welcome/verification email."""
    try:
        now = datetime.utcnow()
        client_ip = request.client.host if request.client else None
        client_ua = request.headers.get("user-agent")
        user = User(
            email=user_in.email,
            hashed_password=await get_password_hash_async(user_in.password),
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
        db.commit()
        db.refresh(user)

        # db.refresh() above opens a fresh transaction (autocommit=False, so
        # any query after a commit starts a new one) purely to reload the
        # row - nothing below this line touches `db` again. Left open, that
        # transaction sits on the checked-out connection for the rest of the
        # request (background_tasks.add_task below, then response
        # serialization/transmission) with no further code of ours in that
        # window to release it - exactly the same exposure login()/
        # reset_password() had before they were fixed to close early (see
        # the long comment in login()), except here the window starts even
        # earlier since there's no bcrypt await to wait for first. Closing
        # here returns the connection immediately instead of leaving it to
        # FastAPI's post-return dependency teardown, which a real client
        # disconnect/cancellation at any point in that window can skip
        # entirely (fastapi.concurrency.contextmanager_in_threadpool has no
        # `finally` around its yield).
        if background_tasks:
            verification_token = generate_email_verification_token(user.email)
            logger.info(f"Queuing welcome email for user {user.email}")
            background_tasks.add_task(
                send_welcome_email,
                user.email,
                user.full_name,
                verification_token
            )
            logger.info(f"Welcome email task queued for user {user.email}")

        return APIResponse.success_response(UserResponse.from_orm(user))
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered"
        )

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

    user = db.query(User).filter(User.email == email).first()
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
    user.email_verification_date = datetime.utcnow()
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
        logger.info(f"Queuing verification email resend for user {current_user.email}")
        background_tasks.add_task(
            send_email_verification,
            current_user.email,
            verification_token
        )
        logger.info(f"Verification email resend task queued for user {current_user.email}")

    return APIResponse.success_response({
        "message": "Verification email sent"
    })

@router.post(
    "/login",
    response_model=APIResponse[Token],
    responses={401: {"description": "Incorrect email or password"}},
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Authenticate with email/password and receive access and refresh tokens."""

    user = db.query(User).filter(User.email == form_data.username).first()
    user_id = user.id if user else None
    user_email = user.email if user else None
    hashed_password = user.hashed_password if user else None

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
    if user_id is None or not password_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )

    db.query(User).filter(User.id == user_id).update(
        {User.last_login: datetime.utcnow()}
    )
    db.commit()

    logger.info(f"Generating tokens for user {user_email}")
    access_token = create_access_token(user_id)
    refresh_token = issue_refresh_token(user_id)
    logger.info(f"Tokens generated for user {user_email}")

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
        new_access_token, new_refresh_token = rotate_refresh_token(refresh_in.refresh_token, db)
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
)
async def logout(
    logout_in: LogoutRequest = LogoutRequest(),
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_active_user),
):
    """Log out the current user: blacklist the access token in use and,
    if a refresh token is supplied, revoke its whole token family so it
    (and any refresh token rotated from it) can no longer be redeemed."""
    logout_tokens(token, logout_in.refresh_token)
    return APIResponse.success_response({"message": "Logged out successfully"})

@router.post(
    "/consent",
    response_model=APIResponse[UserResponse],
    responses={400: {"description": "GDPR consent cannot be revoked via this endpoint"}},
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

    # current_user comes from get_current_active_user -> get_current_user,
    # which depends on app.api.deps.get_db - a *different* get_db than the
    # one this route depends on (app.core.database.get_db). Those are two
    # distinct dependency callables, so FastAPI's per-request dependency
    # cache gives each its own DB session: current_user is attached to the
    # security session, not this route's `db`. Mutating current_user and
    # calling this route's db.commit() was a silent no-op - the session
    # holding the pending change was never committed, so consent updates
    # never actually persisted despite the endpoint returning 200 with the
    # (in-memory-only) updated value. Re-querying through `db` attaches the
    # row to the session this route actually commits.
    user = db.query(User).filter(User.id == current_user.id).first()

    setattr(user, f"{consent_type.value}_consent", granted)
    setattr(user, f"{consent_type.value}_consent_date", datetime.utcnow())

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
        "timestamp": datetime.utcnow().isoformat(),
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }]

    db.commit()
    db.refresh(user)
    return APIResponse.success_response(UserResponse.from_orm(user))

@router.get("/data/export", response_model=APIResponse[GDPRExportData])
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
            timestamp=datetime.fromisoformat(entry["timestamp"]),
            ip_address=entry.get("ip_address"),
            user_agent=entry.get("user_agent"),
        )
        for entry in current_user.consent_history
    ]

    now = datetime.utcnow()
    export_metadata = GDPRExport(
        request_id=f"export_{current_user.id}_{now.timestamp()}",
        request_date=now,
        expires_at=now + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS),
        status="completed",
    )
    export = GDPRExportData(
        personal_data=current_user,
        consents=consents,
        addresses=current_user.addresses,
        orders=current_user.orders,
        export_metadata=export_metadata,
    )


    if background_tasks:
        logger.info(f"Queuing GDPR data export email for user {current_user.email}")
        background_tasks.add_task(
            send_gdpr_export_email,
            current_user.email,
            export_metadata
        )
        logger.info(f"GDPR data export email task queued for user {current_user.email}")

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

    # See the identical note in update_consent above: current_user is bound
    # to a different DB session than this route's `db` (get_current_active_user
    # resolves through app.api.deps.get_db, this route through
    # app.core.database.get_db), so mutating current_user directly and
    # committing `db` silently discarded the deactivation/deletion flags -
    # the account was never actually deactivated. Re-query through `db`.
    # (The former `current_user.deletion_reason = deletion.reason` line is
    # also dropped: User has no deletion_reason column, so it only ever set
    # a transient, non-persisted Python attribute.)
    user = db.query(User).filter(User.id == current_user.id).first()
    user.is_active = False
    user.data_deletion_requested = True
    user.data_deletion_date = datetime.utcnow()
    db.commit()

    logger.info(f"Scheduling hard delete for user {user.email} after 1 day")

    return APIResponse.success_response({
        "message": "Account will be permanently deleted within 24 hours",
        "deletion_date": user.data_deletion_date
    })

@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_data(
    current_user: User = Depends(get_current_active_user),
):
    """Retrieve the current authenticated user's profile."""

    return APIResponse.success_response(UserResponse.from_orm(current_user))

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

    logger.info(f"Updating profile for user {current_user.email}")
    logger.debug(f"Received update request: {user_in.model_dump(exclude_unset=True)}")


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
            old_value = getattr(user, field)
            setattr(user, field, new_value)
            logger.info(
                f"Updated {field} for user {user.email}: "
                f"'{old_value}' -> '{new_value}'"
            )

        db.commit()
        logger.info(
            f"Profile updated successfully for user {user.email}. "
            f"Updated fields: {list(update_data.keys())}"
        )


        db.refresh(user)
        return APIResponse.success_response(UserResponse.from_orm(user))

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update profile for user {current_user.email}: {str(e)}")
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

        logger.info(f"Created address with ID: {address.id} for user {current_user.email}")


        created = db.query(Address).get(address.id)
        logger.info(f"Verified address exists: {created and created.id} for user {current_user.email}")

        return APIResponse.success_response(AddressResponse.from_orm(address))
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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

    logger.info(f"Found address {address_id} for update request from user {current_user.email}")

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

    return APIResponse.success_response(AddressResponse.from_orm(address))

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

    logger.info(f"Processing deletion request for address {address_id} from user {current_user.email}")

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

    logger.info(f"Processing set default request for address {default_address.address_id} from user {current_user.email}")

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
        OrderResponse.from_orm(order) for order in orders
    ])

@router.post("/password/reset", response_model=APIResponse[dict])
async def request_password_reset(
    reset_request: PasswordReset,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """Request a password reset email; always succeeds to avoid leaking registered emails."""

    user = db.query(User).filter(User.email == reset_request.email).first()
    if user:
        token = generate_password_reset_token(user.email)
        if background_tasks:
            logger.info(f"Queuing password reset email for user {user.email}")
            background_tasks.add_task(
                send_password_reset_email,
                user.email,
                token
            )
            logger.info(f"Password reset email task queued for user {user.email}")


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

    email = verify_password_reset_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token"
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return APIResponse.success_response({
        "message": "Token is valid",
        "email": email
    })

@router.post(
    "/password/reset/{token}",
    response_model=APIResponse[dict],
    responses={
        400: {"description": "Invalid or expired reset token"},
        404: {"description": "User not found"},
        422: {"description": "Invalid password format"},
    },
)
async def reset_password(
    token: str,
    new_password: PasswordUpdate,
    db: Session = Depends(get_db),
):
    """Reset a user's password using a valid reset token."""

    try:
        email = verify_password_reset_token(token)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token"
            )

        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        user_id = user.id

        # Same reasoning as login(): release the connection before the
        # CPU-bound bcrypt hash, not after, so neither queueing delay nor a
        # mid-await client disconnect can hold/strand it. See the long
        # comment in login() for the full mechanism.
        db.close()

        new_hashed_password = await get_password_hash_async(new_password.new_password)

        db.query(User).filter(User.id == user_id).update(
            {User.hashed_password: new_hashed_password}
        )
        db.commit()

        # Invalidate any access token issued before this reset so a token
        # held by an attacker (the reason the user is resetting) stops
        # working immediately instead of remaining valid until it expires.
        invalidate_tokens_issued_before_now(user_id)

        return APIResponse.success_response({
            "message": "Password has been reset successfully"
        })
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Invalid password format",
                "errors": e.errors()
            }
        )
