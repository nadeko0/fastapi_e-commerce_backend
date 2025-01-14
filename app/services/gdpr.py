from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.user import User
from app.schemas.legal import ConsentUpdate, ConsentHistory, DataRequest
from app.schemas.user import GDPRExport
from app.core.config import settings

class GDPRService:
    def __init__(self, db: Session):
        self.db = db

    def update_user_consent(
        self,
        user: User,
        consent_update: ConsentUpdate,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> Dict[str, bool]:
        """
        Update user's consent preferences and maintain consent history
        """
        current_time = datetime.utcnow()
        
        # Create consent history entry
        consent_entries = []
        
        # Handle marketing consent
        if user.marketing_consent != consent_update.marketing_consent:
            consent_entries.append({
                "consent_type": "marketing",
                "value": consent_update.marketing_consent,
                "timestamp": current_time.isoformat(),
                "ip_address": ip_address,
                "user_agent": user_agent
            })
            user.marketing_consent = consent_update.marketing_consent
            user.marketing_consent_date = current_time

        # Handle privacy policy acceptance
        if user.privacy_policy_accepted != consent_update.privacy_policy_accepted:
            consent_entries.append({
                "consent_type": "privacy_policy",
                "value": consent_update.privacy_policy_accepted,
                "timestamp": current_time.isoformat(),
                "ip_address": ip_address,
                "user_agent": user_agent
            })
            user.privacy_policy_accepted = consent_update.privacy_policy_accepted
            user.privacy_policy_accepted_date = current_time

        # Update consent history
        user.consent_history = (user.consent_history or []) + consent_entries

        try:
            self.db.commit()
            self.db.refresh(user)
            return {
                "marketing": user.marketing_consent,
                "privacy_policy": user.privacy_policy_accepted,

            }
        except Exception as e:
            self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to update consent preferences"
            )

    async def process_data_export(self, user: User) -> str:
        """
        Process a GDPR data export request
        Returns a request ID that can be used to track the export status
        """
        # Generate unique request ID
        request_id = f"export_{user.id}_{datetime.utcnow().timestamp()}"

        try:
            # Send initial confirmation email
            from app.services.email import send_gdpr_request_received
            send_gdpr_request_received(user.email, "export", request_id)

            # Prepare user data for export
            user_data = {
                "personal_info": {
                    "email": user.email,
                    "full_name": user.full_name,
                    "phone": user.phone,
                    "created_at": user.created_at.isoformat(),
                    "last_login": user.last_login.isoformat() if user.last_login else None,
                },
                "consent_history": user.consent_history,
                "addresses": [
                    {
                        "street": addr.street,
                        "city": addr.city,
                        "country": addr.country,
                        "postal_code": addr.postal_code,
                        "is_default": addr.is_default,
                    }
                    for addr in user.addresses
                ],
                "orders": [
                    {
                        "id": order.id,
                        "status": order.status,
                        "created_at": order.created_at.isoformat(),
                        "total_amount": str(order.total_amount),
                        "items": [
                            {
                                "product_id": item.product_id,
                                "quantity": item.quantity,
                                "price": str(item.price),
                            }
                            for item in order.items
                        ],
                    }
                    for order in user.orders
                ],
            }

            # In production, this would be stored in a secure location
            # and made available for download through a secure link
            from app.services.email import send_gdpr_export_email
            export_data = GDPRExport(
                request_id=request_id,
                request_date=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS)
            )
            send_gdpr_export_email(user.email, export_data)

            return request_id

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail="Failed to process data export request"
            )

    async def process_data_deletion(self, user: User) -> str:
        """
        Process a GDPR data deletion request
        Returns a request ID that can be used to track the deletion status
        """
        request_id = f"deletion_{user.id}_{datetime.utcnow().timestamp()}"

        try:
            # Send initial confirmation email
            from app.services.email import send_gdpr_request_received
            send_gdpr_request_received(user.email, "deletion", request_id)

            # Mark user for deletion
            user.data_deletion_requested = True
            user.data_deletion_date = datetime.utcnow()
            
            # In production, this would trigger a background task to:
            # 1. Anonymize user data
            # 2. Delete personal information
            # 3. Maintain minimal records for legal compliance
            
            self.db.commit()

            # Send deletion confirmation email
            from app.services.email import send_gdpr_deletion_confirmation
            send_gdpr_deletion_confirmation(user.email, request_id)

            return request_id

        except Exception as e:
            self.db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Failed to process deletion request"
            )

    def get_consent_status(self, user: User) -> Dict:
        """
        Get current consent status for a user
        """
        return {
            "marketing_consent": user.marketing_consent,
            "privacy_policy_accepted": user.privacy_policy_accepted,
            "consent_history": user.consent_history
        }

    def validate_retention_period(self, user: User) -> bool:
        """
        Validate if user data is within retention period
        """
        if not user.created_at:
            return True
            
        retention_days = user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
        age_in_days = (datetime.utcnow() - user.created_at).days
        return age_in_days <= retention_days
