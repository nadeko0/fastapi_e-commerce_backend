from datetime import datetime
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    blacklist_token,
    validate_token,
    generate_password_reset_token,
    verify_password_reset_token
)
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate
from app.services.email import EmailService

class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.email_service = EmailService()

    def register_user(self, user_in: UserCreate) -> User:
        user = self.db.query(User).filter(User.email == user_in.email).first()
        if user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        hashed_password = get_password_hash(user_in.password)
        user = User(
            email=user_in.email,
            hashed_password=hashed_password,
            full_name=user_in.full_name,
            phone=user_in.phone,
            gdpr_consent=user_in.gdpr_consent,
            gdpr_consent_date=datetime.utcnow() if user_in.gdpr_consent else None,
            privacy_policy_accepted=user_in.privacy_policy_accepted,
            privacy_policy_accepted_date=datetime.utcnow() if user_in.privacy_policy_accepted else None,
            marketing_consent=user_in.marketing_consent,
            marketing_consent_date=datetime.utcnow() if user_in.marketing_consent else None,
            consent_history=[{
                "type": "initial_consent",
                "gdpr": user_in.gdpr_consent,
                "privacy": user_in.privacy_policy_accepted,
                "marketing": user_in.marketing_consent,
                "timestamp": datetime.utcnow().isoformat()
            }]
        )

        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)

        return user

    def authenticate(self, email: str, password: str) -> Optional[User]:
        user = self.db.query(User).filter(User.email == email).first()
        if not user or not verify_password(password, user.hashed_password):
            return None
        return user

    def login(self, email: str, password: str) -> Tuple[str, str, User]:
        user = self.authenticate(email, password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Inactive user"
            )

        user.last_login = datetime.utcnow()
        self.db.commit()

        access_token = create_access_token(user.id)
        refresh_token = create_refresh_token(user.id)

        return access_token, refresh_token, user

    def refresh_token(self, refresh_token: str) -> Tuple[str, str]:
        payload = validate_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_id = payload.get("sub")
        user = self.db.query(User).filter(User.id == int(user_id)).first()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        blacklist_token(refresh_token, 60 * 60 * 24 * 7)

        new_access_token = create_access_token(user.id)
        new_refresh_token = create_refresh_token(user.id)

        return new_access_token, new_refresh_token

    def logout(self, access_token: str, refresh_token: str) -> bool:
        access_payload = validate_token(access_token)
        if access_payload:
            exp = datetime.fromtimestamp(access_payload.get("exp", 0))
            ttl = int((exp - datetime.utcnow()).total_seconds())
            if ttl > 0:
                blacklist_token(access_token, ttl)

        refresh_payload = validate_token(refresh_token)
        if refresh_payload:
            exp = datetime.fromtimestamp(refresh_payload.get("exp", 0))
            ttl = int((exp - datetime.utcnow()).total_seconds())
            if ttl > 0:
                blacklist_token(refresh_token, ttl)

        return True

    def update_user_consent(self, user: User, gdpr: bool = None, privacy: bool = None, marketing: bool = None) -> User:
        now = datetime.utcnow()
        consent_update = {
            "type": "consent_update",
            "timestamp": now.isoformat()
        }

        if gdpr is not None:
            user.gdpr_consent = gdpr
            user.gdpr_consent_date = now if gdpr else None
            consent_update["gdpr"] = gdpr

        if privacy is not None:
            user.privacy_policy_accepted = privacy
            user.privacy_policy_accepted_date = now if privacy else None
            consent_update["privacy"] = privacy

        if marketing is not None:
            user.marketing_consent = marketing
            user.marketing_consent_date = now if marketing else None
            consent_update["marketing"] = marketing

        user.consent_history.append(consent_update)
        self.db.commit()
        return user

    def request_password_reset(self, email: str) -> bool:
        user = self.db.query(User).filter(User.email == email).first()
        if not user or not user.is_active:
            return True

        reset_token = generate_password_reset_token(email)
        self.email_service.send_password_reset_email(email, reset_token)
        return True

    def reset_password(self, token: str, new_password: str) -> bool:
        email = verify_password_reset_token(token)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reset token"
            )

        user = self.db.query(User).filter(User.email == email).first()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid reset token"
            )

        user.hashed_password = get_password_hash(new_password)
        self.db.commit()
        return True