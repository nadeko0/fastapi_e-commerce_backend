from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

# Reused rather than redefined: app.schemas.user.ConsentHistory has the
# identical shape (both were unified to match the one
# User.consent_history entry format written by app/api/v1/users.py and
# app/services/gdpr.py) - two classes with the same name and shape in
# different modules also collided in the generated OpenAPI schema
# (FastAPI disambiguates same-named schemas with an ugly
# module-qualified name), which this avoids.
from app.schemas.common import RequestBody, input_schema
from app.schemas.user import ConsentHistory


class ConsentUpdate(RequestBody):
    marketing_consent: bool
    privacy_policy_accepted: bool

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "marketing_consent": True,
                "privacy_policy_accepted": True
            }]
        }
    )

class UserConsent(BaseModel):
    marketing_consent: bool
    marketing_consent_date: datetime | None
    privacy_policy_accepted: bool
    privacy_policy_accepted_date: datetime | None
    consent_history: list[ConsentHistory]

    model_config = ConfigDict(from_attributes=True)

class DataRequest(RequestBody):
    request_type: str  # "export" or "deletion" (the route answers 400 to any other)
    reason: str | None = None
    # The account's current password. Required for "deletion": erasure
    # cannot be undone after the grace period, and a bearer token alone (a
    # stolen session) must not be enough for it. Not needed for "export".
    password: str | None = None

    @model_validator(mode="after")
    def require_password_for_deletion(self):
        if self.request_type == "deletion" and not self.password:
            raise ValueError("password is required for a deletion request")
        return self

    model_config = ConfigDict(
        json_schema_extra=input_schema(
            rules={"request_type": {"enum": ["export", "deletion"]}},
            model={
                "examples": [{
                    "request_type": "deletion",
                    "reason": "No longer using the shop",
                    "password": "your-current-password"
                }],
                # require_password_for_deletion, as JSON Schema.
                "if": {"properties": {"request_type": {"const": "deletion"}}, "required": ["request_type"]},
                "then": {"required": ["password"], "properties": {"password": {"type": "string", "minLength": 1}}},
            },
        )
    )

class DataRequestResponse(BaseModel):
    request_id: str
    status: str
    estimated_completion_time: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "request_id": "req_123456",
                "status": "processing",
                "estimated_completion_time": "2025-01-15T19:21:32.388Z"
            }]
        }
    )

class LegalDocument(BaseModel):
    content: str
    version: str
    last_updated: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "content": "Privacy Policy content...",
                "version": "1.0",
                "last_updated": "2025-01-14T19:21:32.388Z"
            }]
        }
    )

class ConsentUpdateResponse(BaseModel):
    status: str
    updated_consents: dict[str, bool]
    timestamp: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{
                "status": "success",
                "updated_consents": {
                    "marketing": True,
                    "privacy_policy": True
                },
                "timestamp": "2025-01-14T19:21:32.388Z"
            }]
        }
    )

class DataRetentionStatus(BaseModel):
    within_retention_period: bool
    retention_days: int
