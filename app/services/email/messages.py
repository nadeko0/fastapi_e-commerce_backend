from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import quote

import app.services.email as email
from app.core.clock import as_utc, utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.product import Product
from app.services.email.templates import render

if TYPE_CHECKING:
    from app.schemas.order import OrderResponse

# The pages the frontend must serve: the links in the verification and
# reset emails open them, and each reads the token from its query string
# and sends it in the JSON body of POST /users/verify-email and
# POST /users/password/reset/confirm (with the new password). The API has no
# GET for a token.
VERIFY_EMAIL_PAGE_PATH = "/verify-email"
RESET_PASSWORD_PAGE_PATH = "/reset-password"
# The page the export-ready mail links to (<path>/<request_id>, no token):
# after login it downloads GET /legal/data-export/{request_id}.
DATA_EXPORT_PAGE_PATH = "/account/data-export"


def _frontend_link(page_path: str, token: str) -> str:
    # The token goes in the query string because that is how the OWASP
    # Forgot Password Cheat Sheet has the link carry it ("attach it in the
    # URL query string"). A fragment would keep it out of server logs and
    # Referer, but the cheat sheet does not describe it; the leak it
    # names is closed by the page sending Referrer-Policy: no-referrer,
    # and by tokens being single use and short lived. quote() with no safe
    # characters encodes everything that could end the value early. The
    # base is FRONTEND_URL, validated without a trailing slash, never the
    # request's Host header.
    return f"{settings.FRONTEND_URL}{page_path}?token={quote(token, safe='')}"


def _utc_text(moment: datetime) -> str:
    # A bare "2026-10-25 01:30:00" reads as the customer's local time. The
    # stored instant is UTC, so it is written in UTC with the zone marked,
    # like the other timestamps in these emails.
    return as_utc(moment).strftime('%Y-%m-%d %H:%M:%S UTC')


def send_order_confirmation_email(to_email: str, order: "OrderResponse") -> bool:
    # order is always an OrderResponse (Pydantic), not the SQLAlchemy Order
    # model: the caller in app/api/v1/orders/ snapshots it via
    # OrderResponse.model_validate(order) before handing off to a FastAPI
    # BackgroundTask, since by the time that task runs the request's DB
    # session is already closed and lazy-loading order.items[i].product
    # would raise DetachedInstanceError. OrderItemResponse carries a
    # denormalized product_name instead of a live product relationship.
    # product_name originates from Product.name (admin-entered, no character
    # validation beyond length) and is rendered into an HTML email a customer
    # opens (autoescaped by the Jinja2 template so markup cannot inject
    # HTML/JS into the message - same reasoning as full_name in
    # send_welcome_email). quantity and price_at_time are server-side
    # numerics. Amounts are in the order's own currency (Order.currency, the
    # store's when it was placed): every order email used to print "$" before
    # them, so a EUR or PLN store told its customers they had paid dollars.
    #
    # Same str-or-enum handling as send_order_status_update_email below, and
    # for the same reason: a freshly-queried Order's .status is a plain str.
    # Fixed there first and missed here, so every order confirmation sent
    # from a Celery task, as they were then, raised AttributeError - found by running a real worker against
    # real orders, which the unit test could not catch because its fake order
    # always carried an enum-like status.
    status_value = order.status.value if hasattr(order.status, "value") else order.status
    html_content = render(
        "order_confirmation.html",
        order_id=order.id,
        items=order.items,
        total_amount=order.total_amount,
        currency=order.currency,
        status=status_value,
        order_date=_utc_text(order.created_at),
    )

    return email.get_email_provider().send(
        to_email,
        f"Order Confirmation #{order.id}",
        html_content,
    )


def send_order_status_update_email(
    to_email: str, order: "Order | OrderResponse | SimpleNamespace"
) -> bool:
    # order.status.value assumed order.status is always an OrderStatus enum
    # member. True for every real call site (app/api/v1/orders/ always
    # passes OrderResponse, whose Pydantic field coerces the DB string into
    # the enum) but not for the raw SQLAlchemy Order this function is
    # type-hinted to accept: Order.status is a bare String column (see
    # app/models/order.py), so a caller handing this a
    # freshly-queried Order got a plain str back and `.value` raised AttributeError.
    status_value = getattr(order.status, "value", order.status)
    html_content = render(
        "order_status_update.html",
        order_id=order.id,
        status=status_value,
        order_date=_utc_text(order.created_at),
        total_amount=order.total_amount,
        currency=order.currency,
    )

    return email.get_email_provider().send(
        to_email,
        f"Order Status Update - #{order.id}",
        html_content,
    )


def send_order_cancellation_email(
    to_email: str, order: "Order | OrderResponse | SimpleNamespace"
) -> bool:
    html_content = render(
        "order_cancellation.html",
        order_id=order.id,
        order_date=_utc_text(order.created_at),
        total_amount=order.total_amount,
        currency=order.currency,
    )

    return email.get_email_provider().send(
        to_email,
        f"Order Cancellation - #{order.id}",
        html_content,
    )


def send_low_stock_alert_email(to_email: str, products: Sequence[Product]) -> bool:
    # product.name is admin-entered and unvalidated for markup; the Jinja2
    # template autoescapes it before rendering into the HTML alert email
    # (defense in depth, mirrors the order-confirmation and welcome emails).
    html_content = render(
        "low_stock_alert.html",
        products=products,
        generated_at=utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    return email.get_email_provider().send(
        to_email,
        "Low Stock Alert",
        html_content,
    )


def send_welcome_email(to_email: str, full_name: str | None, verification_token: str) -> bool:
    verification_link = _frontend_link(VERIFY_EMAIL_PAGE_PATH, verification_token)
    # full_name is attacker-controlled (the caller-supplied name at
    # registration, app/api/v1/users/auth.py) and was previously interpolated
    # into this HTML email unescaped - a name like
    # `<img src=x onerror=...>` would be embedded verbatim into the HTML
    # body an email client renders. The Jinja2 template autoescapes it,
    # matching how every other HTML email in this module only ever renders
    # escaped or server-controlled values. full_name is optional at
    # registration (UserCreate): html.escape(None) used to raise, the
    # background task died and the verification link - required for checkout -
    # was never sent.
    recipient_name = full_name or "customer"
    html_content = render(
        "welcome.html",
        project_name=settings.PROJECT_NAME,
        full_name=recipient_name,
        verification_link=verification_link,
        expire_hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS,
    )

    return email.get_email_provider().send(
        to_email,
        f"Welcome to {settings.PROJECT_NAME}! Please Verify Your Email",
        html_content,
    )


def send_account_exists_email(to_email: str) -> bool:
    """Sent instead of a welcome email when someone registers with an email
    that already has an account. Registration answers both cases the same
    way (so it cannot be used to find out which emails are registered);
    this email is how the owner of the mailbox, and only they, learn which
    case it was. Nothing in it comes from the request."""
    html_content = render(
        "account_exists.html",
        project_name=settings.PROJECT_NAME,
    )

    return email.get_email_provider().send(
        to_email,
        f"You already have an account at {settings.PROJECT_NAME}",
        html_content,
    )


def send_email_verification(to_email: str, verification_token: str) -> bool:
    verification_link = _frontend_link(VERIFY_EMAIL_PAGE_PATH, verification_token)
    html_content = render(
        "email_verification.html",
        verification_link=verification_link,
        expire_hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS,
        project_name=settings.PROJECT_NAME,
    )

    return email.get_email_provider().send(
        to_email,
        "Verify Your Email Address",
        html_content,
    )


def send_password_reset_email(to_email: str, reset_token: str) -> bool:
    reset_link = _frontend_link(RESET_PASSWORD_PAGE_PATH, reset_token)
    html_content = render(
        "password_reset.html",
        reset_link=reset_link,
        expire_minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
        project_name=settings.PROJECT_NAME,
    )

    return email.get_email_provider().send(
        to_email,
        "Password Reset Request",
        html_content,
    )


def send_gdpr_deletion_confirmation(to_email: str, request_id: str) -> bool:
    html_content = render(
        "gdpr_deletion_confirmation.html",
        request_id=request_id,
        scheduled_at=utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        dpo_email=settings.DPO_EMAIL,
        project_name=settings.PROJECT_NAME,
        dpo_name=settings.DPO_NAME,
        company_address=settings.COMPANY_ADDRESS,
    )

    return email.get_email_provider().send(
        to_email,
        "Your Data Deletion Request Has Been Scheduled",
        html_content,
    )


def send_gdpr_deletion_completed(to_email: str, retained_records: list[str] | None = None) -> bool:
    html_content = render(
        "gdpr_deletion_completed.html",
        completed_at=utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        retained_records=retained_records,
        dpo_email=settings.DPO_EMAIL,
        project_name=settings.PROJECT_NAME,
        dpo_name=settings.DPO_NAME,
        company_address=settings.COMPANY_ADDRESS,
    )

    return email.get_email_provider().send(
        to_email,
        "Your Personal Data Has Been Deleted",
        html_content,
    )


def send_gdpr_request_received(to_email: str, request_type: str, request_id: str) -> bool:
    request_type_text = "deletion" if request_type == "deletion" else "export"
    gdpr_article = "17" if request_type == "deletion" else "15"

    html_content = render(
        "gdpr_request_received.html",
        gdpr_article=gdpr_article,
        request_type_text=request_type_text,
        request_id=request_id,
        received_at=utc_now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        dpo_email=settings.DPO_EMAIL,
        project_name=settings.PROJECT_NAME,
        dpo_name=settings.DPO_NAME,
        company_address=settings.COMPANY_ADDRESS,
    )

    return email.get_email_provider().send(
        to_email,
        f"GDPR {request_type.title()} Request Received",
        html_content,
    )


def send_gdpr_export_ready(to_email: str, request_id: str, expires_at: datetime) -> bool:
    """Notify the user that their GDPR Article 15 personal data export is ready to download."""
    download_url = f"{settings.FRONTEND_URL}{DATA_EXPORT_PAGE_PATH}/{request_id}"
    expiry_text = _utc_text(expires_at)

    html_content = render(
        "gdpr_export_ready.html",
        download_url=download_url,
        expiry_text=expiry_text,
        dpo_email=settings.DPO_EMAIL,
        project_name=settings.PROJECT_NAME,
        dpo_name=settings.DPO_NAME,
        company_address=settings.COMPANY_ADDRESS,
    )

    return email.get_email_provider().send(
        to_email,
        "Your personal data export is ready",
        html_content,
    )
