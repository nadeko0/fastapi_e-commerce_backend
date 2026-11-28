from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.cart import guest_cart_cookie_name
from app.core.audit import audit
from app.core.clock import utc_now
from app.core.config import settings
from app.core.dependencies import Cache, CurrentUser, DbSession
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
from app.services.gdpr import GDPRService, consent_user_agent, export_state, find_export
from app.services.gdpr_export import export_path
from app.services.legal_documents import load_legal_document

router = APIRouter(prefix="/legal", tags=["legal"], route_class=DocumentedRoute)


def _render_legal_document(name: str, **context: object) -> LegalDocument:
    doc = load_legal_document(name, allowed_placeholders=(*context, "last_updated"))
    last_updated = doc.effective_date.strftime("%Y-%m-%d")
    return LegalDocument(
        content=doc.text.format(**context, last_updated=last_updated),
        version=doc.version,
        last_updated=doc.effective_date,
    )


@router.get("/privacy-policy", response_model=LegalDocument)
def get_privacy_policy():
    """
    Retrieve the current privacy policy.
    """
    return _render_legal_document(
        "privacy-policy",
        company_name=settings.PROJECT_NAME,
        company_address=settings.COMPANY_ADDRESS,
        company_vat=settings.COMPANY_VAT,
        dpo_name=settings.DPO_NAME,
        dpo_email=settings.DPO_EMAIL,
        technical_contact=settings.TECHNICAL_CONTACT,
        company_email=settings.COMPANY_EMAIL,
        retention_days=settings.USER_DATA_RETENTION_DAYS,
    )


@router.get("/terms-of-service", response_model=LegalDocument)
def get_terms_of_service():
    """
    Retrieve the current terms of service.
    """
    return _render_legal_document(
        "terms-of-service",
        company_name=settings.PROJECT_NAME,
        company_address=settings.COMPANY_ADDRESS,
        company_vat=settings.COMPANY_VAT,
        company_email=settings.COMPANY_EMAIL,
        technical_contact=settings.TECHNICAL_CONTACT,
        dpo_name=settings.DPO_NAME,
        store_currency=settings.STORE_CURRENCY,
    )


@router.get("/cookie-policy", response_model=LegalDocument)
def get_cookie_policy():
    """
    Retrieve the current cookie policy.
    """
    return _render_legal_document(
        "cookie-policy",
        company_name=settings.PROJECT_NAME,
        company_address=settings.COMPANY_ADDRESS,
        technical_contact=settings.TECHNICAL_CONTACT,
        dpo_email=settings.DPO_EMAIL,
        guest_cart_cookie=guest_cart_cookie_name(),
        guest_cart_days=settings.REDIS_GUEST_CART_TTL_DAYS,
    )

async def _load_user(db: AsyncSession, user_id: int) -> User:
    """Load the authenticated user in this route's session.

    current_user (from get_current_active_user) is a detached snapshot:
    get_current_user expunges it, so changing it and committing `db`
    persists nothing. The routes depend on database.get_async_db, the very
    callable get_current_user uses, so FastAPI's per-request dependency cache
    hands both the same AsyncSession (idle at route start): loading the row
    here attaches it to that session, and the service's commit stores the
    change. Same pattern as update_current_user in app/api/v1/users/profile.py.
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
        # app/api/v1/users/auth.py) guards against that instead of assuming
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
        401: {"description": "Incorrect password (deletion)"},
        404: {"description": "User not found (the account was removed after the token was issued)"},
        409: {"description": "Deletion requested by the only active administrator"},
        422: {"description": "request_type is neither export nor deletion, or the password is missing for a "
                             "deletion request"},
        429: {"description": "Too many requests from this address (RATE_LIMIT_AUTH, 20/minute by default), or, "
                             "for a deletion, too many failed password checks "
                             "(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS and "
                             "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL, shared with login)"},
        500: {"description": "The export or the deletion could not be processed"},
        503: {"description": "Failed password attempts cannot be counted right now, or an export cannot be "
                             "queued right now (Retry-After: 30), or no password check slot within BCRYPT_WAIT_TIMEOUT_SECONDS (Retry-After: 5); retry later"},
    },
)
async def request_personal_data(
    request_data: DataRequest,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    redis: Cache,
    background_tasks: BackgroundTasks,
):
    """
    Handle GDPR data access and deletion requests.

    This endpoint allows users to:
    * Request export of their personal data
    * Request deletion of their account and personal data - with the
      account's current password

    The process is asynchronous and returns a request ID for tracking.

    `request_type` is `export` or `deletion` (since 6.0.0 any other value is
    a 422 from the schema, before 6.0.0 it was a 400). An export is built in
    the background as a zip archive; a mail tells the account when it can be
    downloaded from `GET /legal/data-export/{request_id}`, for
    GDPR_EXPORT_EXPIRY_HOURS (24 by default). While one is being built or can
    still be downloaded, a new export request answers its request_id again. A deletion deactivates the account at once (it can no longer
    log in) and the account is anonymised after DATA_DELETION_GRACE_PERIOD_DAYS
    (1 by default); it needs the account's password, and the only active
    administrator cannot be erased (409). The account's new, unpaid orders
    are cancelled with it, their stock returned and their open payment
    attempts closed at the provider; paid orders are kept. It also deletes the account's personal data exports at once: a ready archive cannot be downloaded or recovered afterwards, and one still being built is discarded. Limited per client address by
    RATE_LIMIT_AUTH (20/minute by default).

    The confirmation mails are sent after the response: once the request is
    recorded, a mail that cannot be sent does not turn the answer into an
    error.
    """
    gdpr_service = GDPRService(db)

    if request_data.request_type == "export":
        # Reads user.id and user.email only - the detached snapshot will do.
        request_id = await gdpr_service.process_data_export(current_user, background_tasks)
        audit("gdpr.export_requested", user_id=current_user.id, request_id=request_id)
    else:
        # "deletion", the schema's only other value. Re-authentication. The
        # schema has already refused a deletion request without a password
        # (422). Counted against the same
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
            await _load_user(db, current_user.id), redis, background_tasks
        )
        audit("gdpr.deletion_requested", user_id=current_user.id, request_id=request_id)

    return DataRequestResponse(
        request_id=request_id,
        status="processing",
        estimated_completion_time=utc_now() + timedelta(hours=24)
    )

@router.get(
    "/data-export/{request_id}",
    response_class=FileResponse,
    responses={
        200: {"description": "The export archive (zip: account, addresses, orders, payments, refunds as JSON)",
              "content": {"application/zip": {}}},
        404: {"description": "No such export of this account, or it failed or expired; request a new one"},
        409: {"description": "The export is still being built (Retry-After: 60)"},
    },
)
async def download_personal_data(
    request_id: Annotated[str, Path(pattern=r"^export_[0-9a-f]{32}$")],
    current_user: CurrentUser,
) -> FileResponse:
    """
    Download a personal data export (GDPR Art. 15/20) requested with
    `POST /legal/data-request`. Only the account that requested it can
    download it, until GDPR_EXPORT_EXPIRY_HOURS (24 by default) after it was
    built; the link in the mail leads here through the site, after login.
    Once the account requests its deletion, its exports are deleted and this
    answers 403 (the account is inactive): they cannot be recovered.
    """
    entry = find_export(current_user, request_id)
    state = "gone" if entry is None else export_state(entry, utc_now())
    if state == "building":
        raise HTTPException(status_code=409, detail="The export is still being built",
                            headers={"Retry-After": "60"})
    path = export_path(request_id)
    if state == "gone" or not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    # no-store: personal data, not for a shared or browser cache.
    return FileResponse(path, media_type="application/zip", filename=f"{request_id}.zip",
                        headers={"Cache-Control": "no-store"})

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
