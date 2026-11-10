from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_now
from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession
from app.core.openapi import DocumentedRoute
from app.core.rate_limit import check_consent_change, check_login_attempt, clear_login_failures
from app.core.security import verify_password_async
from app.models.user import User
from app.schemas.legal import (
    ConsentUpdate,
    ConsentUpdateResponse,
    DataRequest,
    DataRequestResponse,
    DataRetentionStatus,
    LegalDocument,
    UserConsent,
)
from app.services.gdpr import GDPRService, consent_user_agent

router = APIRouter(prefix="/legal", tags=["legal"], route_class=DocumentedRoute)

# Legal document content - In production, these should be stored in a database or CMS
# Version and effective date are fixed per document: change them together with
# the text below, never per request.
PRIVACY_POLICY_VERSION = "1.0"
PRIVACY_POLICY_EFFECTIVE = datetime(2026, 11, 10, tzinfo=UTC)
TERMS_OF_SERVICE_VERSION = "1.0"
TERMS_OF_SERVICE_EFFECTIVE = datetime(2026, 11, 10, tzinfo=UTC)
COOKIE_POLICY_VERSION = "1.0"
COOKIE_POLICY_EFFECTIVE = datetime(2026, 11, 10, tzinfo=UTC)

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
- All prices are in {store_currency}
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

@router.get("/privacy-policy", response_model=LegalDocument)
def get_privacy_policy():
    """
    Retrieve the current privacy policy.
    """
    return LegalDocument(
        content=PRIVACY_POLICY.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            company_vat=settings.COMPANY_VAT,
            dpo_name=settings.DPO_NAME,
            dpo_email=settings.DPO_EMAIL,
            technical_contact=settings.TECHNICAL_CONTACT,
            company_email=settings.COMPANY_EMAIL,
            retention_days=settings.USER_DATA_RETENTION_DAYS,
            last_updated=PRIVACY_POLICY_EFFECTIVE.strftime("%Y-%m-%d")
        ),
        version=PRIVACY_POLICY_VERSION,
        last_updated=PRIVACY_POLICY_EFFECTIVE
    )

@router.get("/terms-of-service", response_model=LegalDocument)
def get_terms_of_service():
    """
    Retrieve the current terms of service.
    """
    return LegalDocument(
        content=TERMS_OF_SERVICE.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            company_vat=settings.COMPANY_VAT,
            company_email=settings.COMPANY_EMAIL,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_name=settings.DPO_NAME,
            store_currency=settings.STORE_CURRENCY,
            last_updated=TERMS_OF_SERVICE_EFFECTIVE.strftime("%Y-%m-%d")
        ),
        version=TERMS_OF_SERVICE_VERSION,
        last_updated=TERMS_OF_SERVICE_EFFECTIVE
    )

@router.get("/cookie-policy", response_model=LegalDocument)
def get_cookie_policy():
    """
    Retrieve the current cookie policy.
    """
    return LegalDocument(
        content=COOKIE_POLICY.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_email=settings.DPO_EMAIL,
            essential_cookies=", ".join(settings.ESSENTIAL_COOKIES),
            cookie_consent_days=settings.COOKIE_CONSENT_EXPIRE_DAYS,
            last_updated=COOKIE_POLICY_EFFECTIVE.strftime("%Y-%m-%d")
        ),
        version=COOKIE_POLICY_VERSION,
        last_updated=COOKIE_POLICY_EFFECTIVE
    )

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


@router.post(
    "/consent",
    response_model=ConsentUpdateResponse,
    responses={
        404: {"description": "User not found (the account was removed after the token was issued)"},
        429: {"description": "Too many requests, or too many consent changes: limited per user by "
                             "RATE_LIMIT_CONSENT (10/hour by default), shared with PATCH/PUT /users/me; "
                             "wait Retry-After seconds"},
        500: {"description": "Failed to update consent preferences"},
    },
)
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

    Every call counts against RATE_LIMIT_CONSENT (10/hour by default, per
    user), whether or not a value changes; the same limit covers a change of
    `marketing_consent` through PATCH/PUT /users/me. Over the limit the
    answer is 429 with Retry-After.
    """
    await check_consent_change(current_user.id)
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
        user_agent=consent_user_agent(request),
    )

    return ConsentUpdateResponse(
        status="success",
        updated_consents=updated_consents,
        timestamp=utc_now()
    )

@router.post(
    "/data-request",
    response_model=DataRequestResponse,
    responses={
        400: {"description": "request_type is neither export nor deletion"},
        401: {"description": "Incorrect password (deletion)"},
        404: {"description": "User not found (the account was removed after the token was issued)"},
        409: {"description": "Deletion requested by the only active administrator"},
        422: {"description": "Password missing for a deletion request"},
        429: {"description": "Too many requests from this address (RATE_LIMIT_AUTH, 20/minute by default), or, "
                             "for a deletion, too many failed password checks "
                             "(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS and "
                             "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL, shared with login)"},
        500: {"description": "The export or the deletion could not be processed"},
        503: {"description": "Failed password attempts cannot be counted right now (Retry-After: 30), retry later"},
    },
)
async def request_personal_data(
    request_data: DataRequest,
    request: Request,
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

    `request_type` is `export` or `deletion`. An export is sent to the
    account's email address, valid for GDPR_EXPORT_EXPIRY_HOURS (24 by
    default). A deletion deactivates the account at once (it can no longer
    log in) and the account is anonymised after DATA_DELETION_GRACE_PERIOD_DAYS
    (1 by default); it needs the account's password, and the only active
    administrator cannot be erased (409). Limited per client address by
    RATE_LIMIT_AUTH (20/minute by default).
    """
    gdpr_service = GDPRService(db)

    if request_data.request_type == "export":
        # Reads user.id and user.email only - the detached snapshot will do.
        request_id = await gdpr_service.process_data_export(current_user)
    elif request_data.request_type == "deletion":
        # Re-authentication. The schema has already refused a deletion
        # request without a password (422). Counted against the same
        # failed-attempt limits as /users/login, or a stolen token would be a
        # password oracle with no lockout.
        await check_login_attempt(current_user.email, request)
        assert request_data.password is not None, "DataRequest validator enforces password for deletion"
        if not await verify_password_async(request_data.password, current_user.hashed_password):
            raise HTTPException(
                status_code=401,
                detail="Incorrect password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await clear_login_failures(current_user.email, request)
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

    return DataRequestResponse(
        request_id=request_id,
        status="processing",
        estimated_completion_time=utc_now() + timedelta(hours=24)
    )

@router.get("/consent-status", response_model=UserConsent)
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
    return UserConsent(**gdpr_service.get_consent_status(current_user))

@router.get("/data-retention", response_model=DataRetentionStatus)
async def check_data_retention(
    current_user: CurrentUser
):
    """
    Check if user data is within the retention period.
    """
    gdpr_service = GDPRService()
    is_valid = gdpr_service.validate_retention_period(current_user)
    return DataRetentionStatus(
        within_retention_period=is_valid,
        retention_days=current_user.data_retention_period or settings.USER_DATA_RETENTION_DAYS,
    )
