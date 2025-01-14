from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict

class ConsentUpdate(BaseModel):
    marketing_consent: bool
    privacy_policy_accepted: bool

    class Config:
        schema_extra = {
            "example": {
                "marketing_consent": True,
                "privacy_policy_accepted": True
            }
        }

class ConsentHistory(BaseModel):
    consent_type: str
    value: bool
    timestamp: datetime
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    class Config:
        schema_extra = {
            "example": {
                "consent_type": "marketing",
                "value": True,
                "timestamp": "2025-01-14T19:21:32.388Z",
                "ip_address": "192.168.1.1",
                "user_agent": "Mozilla/5.0..."
            }
        }

class UserConsent(BaseModel):
    marketing_consent: bool
    marketing_consent_date: Optional[datetime]
    privacy_policy_accepted: bool
    privacy_policy_accepted_date: Optional[datetime]
    consent_history: List[ConsentHistory]

    class Config:
        orm_mode = True

class DataRequest(BaseModel):
    request_type: str  # "export" or "deletion"
    reason: Optional[str] = None

    class Config:
        schema_extra = {
            "example": {
                "request_type": "export",
                "reason": "Personal records review"
            }
        }

class DataRequestResponse(BaseModel):
    request_id: str
    status: str
    estimated_completion_time: datetime
    
    class Config:
        schema_extra = {
            "example": {
                "request_id": "req_123456",
                "status": "processing",
                "estimated_completion_time": "2025-01-15T19:21:32.388Z"
            }
        }

class LegalDocument(BaseModel):
    content: str
    version: str
    last_updated: datetime

    class Config:
        schema_extra = {
            "example": {
                "content": "Privacy Policy content...",
                "version": "1.0",
                "last_updated": "2025-01-14T19:21:32.388Z"
            }
        }

class ConsentUpdateResponse(BaseModel):
    status: str
    updated_consents: Dict[str, bool]
    timestamp: datetime

    class Config:
        schema_extra = {
            "example": {
                "status": "success",
                "updated_consents": {
                    "marketing": True,
                    "privacy_policy": True
                },
                "timestamp": "2025-01-14T19:21:32.388Z"
            }
        }