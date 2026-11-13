import logging

from fastapi import HTTPException, Request, status

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.users._router import router
from app.core.clock import utc_now
from app.core.dependencies import CurrentUser, DbSession
from app.core.rate_limit import check_consent_change
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdate
from app.services.gdpr import consent_user_agent

# The name the module had before the users package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.users")


@router.get("/me", response_model=UserResponse)
async def get_current_user_data(
    current_user: CurrentUser,
):
    """Retrieve the current authenticated user's profile."""

    return UserResponse.model_validate(current_user)

# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/me",
    response_model=UserResponse,
    summary="Patch Current User",
    description=(
        PATCH_DESCRIPTION
        + "\n\nAt least one field is required (else 400). `marketing_consent` cannot be null. A change of "
        "`marketing_consent` is recorded in the consent history and counts against RATE_LIMIT_CONSENT "
        "(10/hour by default, per user, shared with POST /legal/consent): over the limit the answer is 429 "
        "with Retry-After. Order placement needs a full name and a phone number."
    ),
    openapi_extra=merge_patch_body("UserUpdate"),
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        429: {"description": "Too many requests, or too many consent changes: a change of marketing_consent is "
                             "limited per user by RATE_LIMIT_CONSENT (10/hour by default), shared with "
                             "POST /legal/consent; sending the value it already has is not counted"},
        500: {"description": "Failed to update profile"},
    },
)
@router.put(
    "/me",
    response_model=UserResponse,
    responses={
        400: {"description": "No fields to update"},
        404: {"description": "User not found"},
        429: {"description": "Too many requests, or too many consent changes: a change of marketing_consent is "
                             "limited per user by RATE_LIMIT_CONSENT (10/hour by default), shared with "
                             "POST /legal/consent; sending the value it already has is not counted"},
        500: {"description": "Failed to update profile"},
    },
)
async def update_current_user(
    user_in: UserUpdate,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on the current authenticated user's profile.

    At least one field is required (else 400). `full_name` and `phone` may be
    cleared with null; `marketing_consent` cannot be null. A change of
    `marketing_consent` is recorded in the consent history and counts
    against RATE_LIMIT_CONSENT (10/hour by default, per user, shared with
    POST /legal/consent): over the limit the answer is 429 with Retry-After.
    Order placement needs a full name and a phone number."""

    # Field names only, never their values: names and phone numbers are
    # personal data, and the logs are kept apart from the account and not
    # scrubbed when it is erased.
    logger.info("Updating profile for user %s", current_user.id)

    update_data = user_in.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )

    logger.info("Fields to update: %s", list(update_data.keys()))

    user = await db.get(User, current_user.id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Outside the try below, which turns every exception into a 500.
    new_consent = update_data.get("marketing_consent")
    if new_consent is not None and new_consent != user.marketing_consent:
        await check_consent_change(user.id)

    try:
        consent = update_data.pop("marketing_consent", None)
        if consent is not None and consent != user.marketing_consent:
            # Art. 7(1): a consent change must be demonstrable. Same entry
            # shape as register_user and GDPRService.update_user_consent.
            now = utc_now()
            user.consent_history = (user.consent_history or []) + [{
                "type": "marketing",
                "granted": consent,
                "timestamp": now.isoformat(),
                "ip_address": request.client.host if request.client else None,
                "user_agent": consent_user_agent(request),
            }]
            user.marketing_consent = consent
            user.marketing_consent_date = now
        for field, new_value in update_data.items():
            setattr(user, field, new_value)

        await db.commit()
        logger.info(
            "Profile updated successfully for user %s. Updated fields: %s", user.id, list(update_data.keys())
        )


        await db.refresh(user)
        return UserResponse.model_validate(user)

    except Exception as e:
        await db.rollback()
        # The exception's type only: its message can quote the values.
        logger.error("Failed to update profile for user %s: %s", current_user.id, type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile"
        ) from e
