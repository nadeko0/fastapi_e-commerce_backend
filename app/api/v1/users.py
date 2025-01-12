import logging
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import ValidationError
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_password,
    get_password_hash,
    get_current_user,
    get_current_active_user,
    generate_password_reset_token,
    verify_password_reset_token,
    generate_email_verification_token,
    verify_email_token,
)
from app.core.database import get_db
from app.schemas.user import (
    UserCreate,
    UserUpdate,
    UserResponse,
    Token,
    PasswordReset,
    PasswordUpdate,
    GDPRExport,
    GDPRDelete,
    ConsentHistory,
    ConsentType,
)
from app.schemas.address import (
    AddressCreate,
    AddressUpdate,
    AddressResponse,
    AddressListResponse,
    SetDefaultAddress,
)
from app.schemas.order import OrderResponse
from app.schemas.common import (
    APIResponse,
    ErrorCode,
    PaginationParams,
    ValidationErrorResponse,
)
from app.models.user import User
from app.models.address import Address
from app.models.order import Order
from app.services.email import (
    send_password_reset_email,
    send_gdpr_export_email,
    send_welcome_email,
    send_email_verification
)
from app.services.redis import RedisService

router = APIRouter(prefix="/users", tags=["users"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="users/login")



@router.post("/register", response_model=APIResponse[UserResponse])
async def register_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    try:
        now = datetime.utcnow()
        user = User(
            email=user_in.email,
            hashed_password=get_password_hash(user_in.password),
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
                "timestamp": now.isoformat()
            }, {
                "type": ConsentType.PRIVACY_POLICY.value,
                "granted": user_in.privacy_policy_accepted,
                "timestamp": now.isoformat()
            }, {
                "type": ConsentType.MARKETING.value,
                "granted": user_in.marketing_consent,
                "timestamp": now.isoformat()
            }]
        )
        db.add(user)
        db.commit()
        db.refresh(user)


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

@router.get("/verify-email/{token}", response_model=APIResponse[dict])
async def verify_email(
    token: str,
    db: Session = Depends(get_db),
):

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

@router.post("/verify-email/resend", response_model=APIResponse[dict])
async def resend_verification_email(
    current_user: User = Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
):

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

@router.post("/login", response_model=APIResponse[Token])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )


    user.last_login = datetime.utcnow()
    db.commit()


    logger.info(f"Generating tokens for user {user.email}")
    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)
    logger.info(f"Tokens generated for user {user.email}")

    return APIResponse.success_response(Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer"
    ))

@router.post("/consent", response_model=APIResponse[UserResponse])
async def update_consent(
    consent_type: ConsentType,
    granted: bool,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    if consent_type == ConsentType.GDPR and not granted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GDPR consent cannot be revoked. Use data deletion instead."
        )

    setattr(current_user, f"{consent_type.value}_consent", granted)
    setattr(current_user, f"{consent_type.value}_consent_date", datetime.utcnow())
    
    # Add to consent history
    current_user.consent_history.append({
        "type": consent_type.value,
        "granted": granted,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    db.commit()
    return APIResponse.success_response(UserResponse.from_orm(current_user))

@router.get("/data/export", response_model=APIResponse[GDPRExport])
async def export_user_data(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):

    consents = [
        ConsentHistory(
            type=entry["type"],
            granted=entry["granted"],
            timestamp=datetime.fromisoformat(entry["timestamp"])
        )
        for entry in current_user.consent_history
    ]


    export = GDPRExport(
        personal_data=current_user,
        consents=consents,
        addresses=current_user.addresses,
        orders=current_user.orders,
        expires_at=datetime.utcnow() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS)
    )


    if background_tasks:
        logger.info(f"Queuing GDPR data export email for user {current_user.email}")
        background_tasks.add_task(
            send_gdpr_export_email,
            current_user.email,
            export
        )
        logger.info(f"GDPR data export email task queued for user {current_user.email}")

    return APIResponse.success_response(export)

@router.post("/data/delete", response_model=APIResponse[dict])
async def delete_user_data(
    deletion: GDPRDelete,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    if not verify_password(deletion.password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password"
        )


    current_user.is_active = False
    current_user.data_deletion_requested = True
    current_user.data_deletion_date = datetime.utcnow()
    current_user.deletion_reason = deletion.reason
    db.commit()

    logger.info(f"Scheduling hard delete for user {current_user.email} after 1 day")

    return APIResponse.success_response({
        "message": "Account will be permanently deleted within 24 hours",
        "deletion_date": current_user.data_deletion_date
    })

@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_data(
    current_user: User = Depends(get_current_active_user),
):

    return APIResponse.success_response(UserResponse.from_orm(current_user))

@router.put("/me", response_model=APIResponse[UserResponse])
async def update_current_user(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    logger.info(f"Updating profile for user {current_user.email}")
    logger.debug(f"Received update request: {user_in.model_dump(exclude_unset=True)}")
    

    update_data = user_in.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )
    
    logger.info(f"Fields to update: {list(update_data.keys())}")
    
    try:

        user = db.query(User).filter(User.id == current_user.id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        

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

    total = db.query(Address).filter(
        Address.user_id == current_user.id,
        Address.is_active == True
    ).count()

    addresses = (
        db.query(Address)
        .filter(
            Address.user_id == current_user.id,
            Address.is_active == True
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

@router.post("/addresses", response_model=APIResponse[AddressResponse])
async def create_address(
    address_in: AddressCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    try:

        active_addresses = db.query(Address).filter(
            Address.user_id == current_user.id,
            Address.is_active == True
        ).all()
    

        should_be_default = address_in.is_default or not active_addresses
        if should_be_default:

            existing_default = db.query(Address).filter(
                Address.user_id == current_user.id,
                Address.is_active == True,
                Address.is_default == True
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

@router.put("/addresses/{address_id}", response_model=APIResponse[AddressResponse])
async def update_address(
    address_id: int,
    address_in: AddressUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

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
            Address.is_active == True,
            Address.is_default == True,
            Address.id != address_id
        ).first()
        if existing_default:
            existing_default.is_default = False
    

    for field, value in address_in.model_dump(exclude_unset=True).items():
        setattr(address, field, value)
    
    db.commit()
    db.refresh(address)
    
    return APIResponse.success_response(AddressResponse.from_orm(address))

@router.delete("/addresses/{address_id}", response_model=APIResponse[dict])
async def delete_address(
    address_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

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
            Address.is_active == True,
            Address.id != address_id
        ).order_by(Address.created_at.desc()).first()
        if new_default:
            new_default.is_default = True
    
    db.commit()
    
    return APIResponse.success_response({
        "message": "Address deleted successfully"
    })

@router.post("/addresses/default", response_model=APIResponse[dict])
async def set_default_address(
    default_address: SetDefaultAddress,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

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
        Address.is_active == True,
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

    orders = (
        db.query(Order)
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

@router.get("/password/reset/{token}", response_model=APIResponse[dict])
async def validate_reset_token(
    token: str,
    db: Session = Depends(get_db),
):

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

@router.post("/password/reset/{token}", response_model=APIResponse[dict])
async def reset_password(
    token: str,
    new_password: PasswordUpdate,
    db: Session = Depends(get_db),
):

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
        
        user.hashed_password = get_password_hash(new_password.new_password)
        db.commit()
        
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