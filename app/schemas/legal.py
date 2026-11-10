from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Reused rather than redefined: app.schemas.user.ConsentHistory has the
# identical shape (both were unified to match the one
# User.consent_history entry format written by app/api/v1/users.py and
# app/services/gdpr.py) - two classes with the same name and shape in
# different modules also collided in the generated OpenAPI schema
# (FastAPI disambiguates same-named schemas with an ugly
# module-qualified name), which this avoids.
from app.schemas.common import RequestBody, input_schema, published_values
from app.schemas.user import ConsentHistory


class ConsentUpdate(RequestBody):
    marketing_consent: bool = Field(description="Whether marketing emails are allowed.")
    privacy_policy_accepted: bool = Field(description="Whether the privacy policy is accepted.")

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
    request_type: Literal["export", "deletion"] = Field(
        description="export or deletion; any other value is a 422."
    )
    reason: str | None = Field(None, description="Optional free text; not stored or acted on.")
    # The account's current password. Required for "deletion": erasure
    # cannot be undone after the grace period, and a bearer token alone (a
    # stolen session) must not be enough for it. Not needed for "export".
    password: str | None = Field(
        None, description="The account's current password; required for deletion (422 without it), not used for export."
    )

    @model_validator(mode="after")
    def require_password_for_deletion(self):
        if self.request_type == "deletion" and not self.password:
            raise ValueError("password is required for a deletion request")
        return self

    model_config = ConfigDict(
        json_schema_extra=input_schema(
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
    # Always "processing": the only status the route (app/api/v1/legal.py) sends.
    status: Annotated[str, published_values(["processing"])]
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
    status: Annotated[str, published_values(["success"])]
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
