import contextlib

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import normalize_email


async def _user_by_email(db: AsyncSession, email: str):
    """The account for this email, in whatever case it was typed or stored.
    lower(email) on the column, not just the normalised input: rows stored
    before emails were normalised keep the spelling their owner typed. The
    comparison is what the unique index on lower(email) covers."""
    canonical = normalize_email(email)
    # The spelling registration stores (EmailStr: IDNA / UTS #46 domain). An
    # address that is not valid is simply not found: a plain 401, like any other.
    with contextlib.suppress(EmailNotValidError):
        canonical = validate_email(canonical, check_deliverability=False).normalized
    result = await db.execute(
        select(User).where(func.lower(User.email) == canonical).limit(1)
    )
    return result.scalars().first()
