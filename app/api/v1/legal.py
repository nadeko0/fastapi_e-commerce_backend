from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_now
from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.core.openapi import DocumentedRoute
from app.core.security import verify_password_async
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.legal import (
    ConsentUpdate,
    ConsentUpdateResponse,
    DataRequest,
    DataRequestResponse,
    LegalDocument,
    UserConsent,
)
from app.services.gdpr import GDPRService

router = APIRouter(prefix="/legal", tags=["Legal & GDPR"], route_class=DocumentedRoute)

# Legal document content - In production, these should be stored in a database or CMS
PRIVACY_POLICY = """
Privacy Policy for {company_name}

Last Updated: {last_updated}

1. Who We Are
{company_name}
Address: {company_address}
VAT: {company_vat}
Data Protection Officer: {dpo_name}
DPO Email: {dpo_email}
Technical Contact: {technical_contact}

2. Legal Basis (GDPR Article 6)
We process your data based on:
- Contract fulfillment (for orders)
- Legal obligations (tax, business records)
- Legitimate interests (security, fraud prevention)
- Your consent (marketing)

3. Data We Process
We collect and process:
- Account information (email, name)
- Order details and history
- Delivery addresses
- Payment information (processed securely via certified providers)
- Technical data (IP address, cookies - see Cookie Policy)

4. How We Use Your Data
- Order processing and delivery
- Account management
- Legal compliance
- Security and fraud prevention
- Service improvements
- Marketing (with consent)

5. Your GDPR Rights
You have the right to:
- Access your data (Article 15)
- Correct your data (Article 16)
- Delete your data (Article 17)
- Port your data (Article 20)
- Withdraw consent (Article 7)
- Object to processing (Article 21)

6. Data Retention
We keep your data for {retention_days} days unless:
- Required longer by law
- You request deletion
- Ongoing legal proceedings

7. International Transfers
We process data within the EU/EEA.
Any transfers outside follow GDPR Chapter 5.

8. Contact
Privacy questions: {dpo_email}
Technical issues: {company_email}
Postal: {company_address}

9. Supervisory Authority
You have the right to complain to your local data protection authority.
"""

TERMS_OF_SERVICE = """
Terms of Service for {company_name}

Last Updated: {last_updated}

1. Company Information
{company_name}
Registered Address: {company_address}
VAT Number: {company_vat}
Email: {company_email}

2. Agreement
By using our service, you agree to these terms.
These terms constitute a legally binding agreement.

3. Account Rules
- Provide accurate information
- Keep your account secure
- Must be 16 or older (GDPR requirement)
- One account per user

4. Orders and Payments
- All prices in EUR including VAT
- Payment required before shipping
- Secure payment processing
- Order confirmation via email

5. EU Consumer Rights
- 14-day withdrawal right (EU Directive 2011/83/EU)
- Right to return goods
- Refund within 14 days of return
- Warranty rights under EU law

6. Data Protection
- GDPR compliance
- Data processing details in Privacy Policy
- Cookie usage detailed in Cookie Policy
- Right to data portability

7. Dispute Resolution
- EU Online Dispute Resolution platform
- Applicable law: EU member state
- Court of jurisdiction: Your residence

8. Contact
Technical: {technical_contact}
Legal/DPO: {dpo_name}
Email: {company_email}
Address: {company_address}
"""

COOKIE_POLICY = """
Cookie Policy for {company_name}

Last Updated: {last_updated}

1. Introduction
This policy explains how {company_name} uses cookies and similar technologies.
We respect your right to privacy and provide cookie controls.

2. What Are Cookies
Cookies are small text files stored on your device.
They help make websites work better and provide information to owners.

3. Cookie Categories
We use only essential cookies for:
- Shopping cart functionality
- Account authentication
- Security measures
- Session management
- CSRF protection

4. Cookie List
Essential Cookies:
{essential_cookies}
Duration: Session to {cookie_consent_days} days

5. Your Controls
- Accept/reject via cookie banner
- Browser settings configuration
- Essential cookies cannot be disabled
- Cookie preferences can be updated anytime

6. Legal Basis
We use cookies based on:
- Essential cookies: Legitimate interest
- All others: Your consent

7. Updates
We may update this policy.
Last updated: {last_updated}

8. Contact
Technical: {technical_contact}
Privacy/DPO: {dpo_email}
Address: {company_address}
"""

@router.get("/privacy-policy", response_model=APIResponse[LegalDocument])
def get_privacy_policy():
    """
    Retrieve the current privacy policy.
    """
    current_time = utc_now()
    return APIResponse.success_response(LegalDocument(
        content=PRIVACY_POLICY.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            company_vat=settings.COMPANY_VAT,
            dpo_name=settings.DPO_NAME,
            dpo_email=settings.DPO_EMAIL,
            technical_contact=settings.TECHNICAL_CONTACT,
            company_email=settings.COMPANY_EMAIL,
            retention_days=settings.USER_DATA_RETENTION_DAYS,
            last_updated=current_time.strftime("%Y-%m-%d")
        ),
        version="1.0",
        last_updated=current_time
    ))

@router.get("/terms-of-service", response_model=APIResponse[LegalDocument])
def get_terms_of_service():
    """
    Retrieve the current terms of service.
    """
    current_time = utc_now()
    return APIResponse.success_response(LegalDocument(
        content=TERMS_OF_SERVICE.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            company_vat=settings.COMPANY_VAT,
            company_email=settings.COMPANY_EMAIL,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_name=settings.DPO_NAME,
            last_updated=current_time.strftime("%Y-%m-%d")
        ),
        version="1.0",
        last_updated=current_time
    ))

@router.get("/cookie-policy", response_model=APIResponse[LegalDocument])
def get_cookie_policy():
    """
    Retrieve the current cookie policy.
    """
    current_time = utc_now()
    return APIResponse.success_response(LegalDocument(
        content=COOKIE_POLICY.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_email=settings.DPO_EMAIL,
            essential_cookies=", ".join(settings.ESSENTIAL_COOKIES),
            cookie_consent_days=settings.COOKIE_CONSENT_EXPIRE_DAYS,
            last_updated=current_time.strftime("%Y-%m-%d")
        ),
        version="1.0",
        last_updated=current_time
    ))

async def _load_user(db: AsyncSession, user_id: int) -> User:
    """Load the authenticated user in this route's session.

    current_user (from get_current_active_user) is a detached snapshot:
    get_current_user expunges it, so changing it and committing `db`
    persists nothing. The routes depend on database.get_async_db, the very
    callable get_current_user uses, so FastAPI's per-request dependency cache
    hands both the same AsyncSession (idle at route start): loading the row
    here attaches it to that session, and the service's commit stores the
    change. Same pattern as update_current_user in app/api/v1/users.py.
    """
    user = await db.get(User, user_id)
    if user is None:
        # Authenticated a moment ago, gone now (hard-deleted in between).
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/consent", response_model=APIResponse[ConsentUpdateResponse])
async def update_user_consent(
    consent_update: ConsentUpdate,
    request: Request,
    db: DbSession,
    current_user: CurrentUser
):
    """
    Update user's consent preferences for marketing and analytics.

    This endpoint allows users to update their consent preferences for:
    * Marketing communications
    * Analytics tracking
    * Privacy policy acceptance

    The endpoint also maintains a complete history of consent changes.
    """
    gdpr_service = GDPRService(db)
    updated_consents = await gdpr_service.update_user_consent(
        # Loaded through `db`: the service mutates this object and commits
        # `db` (see _load_user).
        user=await _load_user(db, current_user.id),
        consent_update=consent_update,
        # request.client can be None (e.g. some ASGI transports/test
        # clients don't set it) - every other endpoint in this codebase that
        # records an IP address (see register_user in
        # app/api/v1/users.py) guards against that instead of assuming
        # .client is always present, which would otherwise raise
        # AttributeError and turn a routine consent update into a 500.
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )

    return APIResponse.success_response(ConsentUpdateResponse(
        status="success",
        updated_consents=updated_consents,
        timestamp=utc_now()
    ))

@router.post(
    "/data-request",
    response_model=APIResponse[DataRequestResponse],
    responses={
        401: {"description": "Incorrect password (deletion)"},
        422: {"description": "Password missing for a deletion request"},
    },
)
async def request_personal_data(
    request_data: DataRequest,
    db: DbSession,
    current_user: CurrentUser
):
    """
    Handle GDPR data access and deletion requests.

    This endpoint allows users to:
    * Request export of their personal data
    * Request deletion of their account and personal data - with the
      account's current password

    The process is asynchronous and returns a request ID for tracking.
    """
    gdpr_service = GDPRService(db)

    if request_data.request_type == "export":
        # Reads user.id and user.email only - the detached snapshot will do.
        request_id = await gdpr_service.process_data_export(current_user)
    elif request_data.request_type == "deletion":
        # Re-authentication. The schema has already refused a deletion
        # request without a password (422).
        if not await verify_password_async(request_data.password, current_user.hashed_password):
            raise HTTPException(
                status_code=401,
                detail="Incorrect password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # Deactivates the user and commits `db`, so it needs the row loaded
        # through `db` (see _load_user).
        request_id = await gdpr_service.process_data_deletion(
            await _load_user(db, current_user.id)
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid request type. Must be either 'export' or 'deletion'"
        )

    return APIResponse.success_response(DataRequestResponse(
        request_id=request_id,
        status="processing",
        estimated_completion_time=utc_now() + timedelta(hours=24)
    ))

@router.get("/consent-status", response_model=APIResponse[UserConsent])
async def get_consent_status(
    current_user: CurrentUser
):
    """
    Retrieve current consent status and history for the user.

    Returns:
    * Current consent settings
    * Timestamps of consent changes
    * Complete consent history
    """
    gdpr_service = GDPRService()
    return APIResponse.success_response(UserConsent(**gdpr_service.get_consent_status(current_user)))

@router.get("/data-retention", response_model=APIResponse[dict])
async def check_data_retention(
    current_user: CurrentUser
):
    """
    Check if user data is within the retention period.
    """
    gdpr_service = GDPRService()
    is_valid = gdpr_service.validate_retention_period(current_user)
    return APIResponse.success_response({
        "within_retention_period": is_valid,
        "retention_days": current_user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
    })
