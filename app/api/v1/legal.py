from fastapi import APIRouter, HTTPException, Depends, Request
from typing import Dict, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import settings
from app.schemas.legal import (
    ConsentUpdate,
    DataRequest,
    DataRequestResponse,
    LegalDocument,
    ConsentUpdateResponse,
    UserConsent
)
from app.services.gdpr import GDPRService

router = APIRouter(prefix="/legal", tags=["Legal & GDPR"])

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

@router.get("/privacy-policy", response_model=LegalDocument)
def get_privacy_policy():
    """
    Retrieve the current privacy policy.
    """
    current_time = datetime.utcnow()
    return {
        "content": PRIVACY_POLICY.format(
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
        "version": "1.0",
        "last_updated": current_time
    }

@router.get("/terms-of-service", response_model=LegalDocument)
def get_terms_of_service():
    """
    Retrieve the current terms of service.
    """
    current_time = datetime.utcnow()
    return {
        "content": TERMS_OF_SERVICE.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            company_vat=settings.COMPANY_VAT,
            company_email=settings.COMPANY_EMAIL,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_name=settings.DPO_NAME,
            last_updated=current_time.strftime("%Y-%m-%d")
        ),
        "version": "1.0",
        "last_updated": current_time
    }

@router.get("/cookie-policy", response_model=LegalDocument)
def get_cookie_policy():
    """
    Retrieve the current cookie policy.
    """
    current_time = datetime.utcnow()
    return {
        "content": COOKIE_POLICY.format(
            company_name=settings.PROJECT_NAME,
            company_address=settings.COMPANY_ADDRESS,
            technical_contact=settings.TECHNICAL_CONTACT,
            dpo_email=settings.DPO_EMAIL,
            essential_cookies=", ".join(settings.ESSENTIAL_COOKIES),
            cookie_consent_days=settings.COOKIE_CONSENT_EXPIRE_DAYS,
            last_updated=current_time.strftime("%Y-%m-%d")
        ),
        "version": "1.0",
        "last_updated": current_time
    }

@router.post("/consent", response_model=ConsentUpdateResponse)
async def update_user_consent(
    consent_update: ConsentUpdate,
    request: Request,
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_user)
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
    updated_consents = gdpr_service.update_user_consent(
        user=current_user,
        consent_update=consent_update,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent")
    )
    
    return ConsentUpdateResponse(
        status="success",
        updated_consents=updated_consents,
        timestamp=datetime.utcnow()
    )

@router.post("/data-request", response_model=DataRequestResponse)
async def request_personal_data(
    request_data: DataRequest,
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_user)
):
    """
    Handle GDPR data access and deletion requests.
    
    This endpoint allows users to:
    * Request export of their personal data
    * Request deletion of their account and personal data
    
    The process is asynchronous and returns a request ID for tracking.
    """
    gdpr_service = GDPRService(db)
    
    if request_data.request_type == "export":
        request_id = await gdpr_service.process_data_export(current_user)
    elif request_data.request_type == "deletion":
        request_id = await gdpr_service.process_data_deletion(current_user)
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid request type. Must be either 'export' or 'deletion'"
        )
    
    return DataRequestResponse(
        request_id=request_id,
        status="processing",
        estimated_completion_time=datetime.utcnow() + timedelta(hours=24)
    )

@router.get("/consent-status", response_model=UserConsent)
async def get_consent_status(
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_user)
):
    """
    Retrieve current consent status and history for the user.
    
    Returns:
    * Current consent settings
    * Timestamps of consent changes
    * Complete consent history
    """
    gdpr_service = GDPRService(db)
    return gdpr_service.get_consent_status(current_user)

@router.get("/data-retention")
async def check_data_retention(
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_user)
):
    """
    Check if user data is within the retention period.
    """
    gdpr_service = GDPRService(db)
    is_valid = gdpr_service.validate_retention_period(current_user)
    return {
        "within_retention_period": is_valid,
        "retention_days": current_user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
    }