import logging
from typing import TYPE_CHECKING, Annotated

from fastapi import HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.convertors import Convertor, register_url_convertor

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.users._router import router
from app.core.dependencies import CurrentUser, DbSession
from app.core.errors import Problem, ProblemType, sanitized_errors
from app.core.location import set_location
from app.core.openapi import LOCATION_HEADER
from app.core.sorting import order_clauses
from app.models.address import Address
from app.models.user import User
from app.schemas.address import (
    AddressCreate,
    AddressListResponse,
    AddressResponse,
    AddressUpdate,
    SetDefaultAddress,
)
from app.schemas.common import (
    Int32,
    Message,
    PaginationParams,
    sortable_pagination,
)

# The name the module had before the users package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.users")


class SignedIntConvertor(Convertor[int]):
    """Starlette's `int` matches digits only, so a negative id would no longer
    reach the route and answer 422 (the Int32 bound) but 404. This one still
    refuses what is not a number, which is what lets the literal
    /addresses/default fall through to its own route."""

    regex = "-?[0-9]+"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(int(value))


register_url_convertor("sint", SignedIntConvertor())


ADDRESS_SORT_COLUMNS = {"id": Address.id, "created_at": Address.created_at}

# Saving an address meets two unique indexes: uix_active_user_address (the
# same address twice) and uix_user_default_address (two defaults, when
# another request made a different address the default since this one read
# them). Both are a 409; PostgreSQL names the index but SQLite does not, so
# one message covers both.
ADDRESS_CONFLICT = (
    "This address already exists for the current user, or another request "
    "changed the default address at the same time"
)
DEFAULT_CONFLICT = "Another request changed the default address at the same time; try again"


async def _lock_user_addresses(db: AsyncSession, user_id: int) -> None:
    """One change to a user's addresses at a time, until the transaction
    ends: two requests moving the default (set-default, create or update with
    is_default, deleting the default) each updated the other's rows and
    could deadlock (SQLSTATE 40P01, a 500). The user's row is the lock, as in
    POST /orders; FOR NO KEY UPDATE leaves foreign-key checks on it free."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))


async def _commit_address_change(db: AsyncSession, conflict: str) -> None:
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict) from e


if TYPE_CHECKING:
    AddressListParams = PaginationParams
else:
    AddressListParams = sortable_pagination("AddressListParams", tuple(ADDRESS_SORT_COLUMNS))


@router.get("/addresses", response_model=AddressListResponse)
async def list_addresses(
    pagination: Annotated[AddressListParams, Query()],
    current_user: CurrentUser,
    db: DbSession,
):
    """List the current user's active addresses, paginated."""

    total = await db.scalar(
        select(func.count()).select_from(Address).where(
            Address.user_id == current_user.id,
            Address.is_active
        )
    )
    assert total is not None, "SELECT count(*) always returns an integer"

    addresses = (await db.execute(
        select(Address)
        .where(
            Address.user_id == current_user.id,
            Address.is_active
        )
        .order_by(*order_clauses(
            pagination.sort_by, pagination.sort_order, ADDRESS_SORT_COLUMNS, Address.id, [Address.id]
        ))
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
    )).scalars().all()

    return AddressListResponse(
        items=addresses,
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > (pagination.page * pagination.size)
    )

@router.get(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    responses={404: {"description": "Address not found, deleted, or not the current user's"}},
)
async def get_address(
    address_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """One of the current user's active addresses.

    The address of another user, a deleted one and a missing one are the
    same 404, so the id says nothing about who owns it."""

    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id,
            Address.is_active
        ).limit(1)
    )).scalars().first()

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )

    return AddressResponse.model_validate(address)

@router.post(
    "/addresses",
    response_model=AddressResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Address created; Location is its URL", "headers": LOCATION_HEADER},
        409: {"description": "This address already exists for the current user (same street, city, state, "
                             "postal code and country as an active address), or another request made a "
                             "different address the default at the same time"},
        422: {"description": "Invalid address data"},
    },
)
async def create_address(
    request: Request,
    response: Response,
    address_in: AddressCreate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Add a new address for the current user.

    The first address, and one sent with `is_default` true, becomes the
    default; the previous default is unset. Whitespace in `postal_code` is
    removed and `country` is normalised (a two-letter code is upper-cased).
    There is no limit on the number of addresses."""

    try:
        await _lock_user_addresses(db, current_user.id)
        active_addresses = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active
            )
        )).scalars().all()


        should_be_default = address_in.is_default or not active_addresses
        if should_be_default:

            existing_default = (await db.execute(
                select(Address).where(
                    Address.user_id == current_user.id,
                    Address.is_active,
                    Address.is_default
                ).limit(1)
            )).scalars().first()
            if existing_default:
                existing_default.is_default = False


        address = Address(
            user_id=current_user.id,
            is_default=should_be_default,
            **address_in.model_dump(exclude={'is_default'})
        )

        db.add(address)
        await _commit_address_change(db, ADDRESS_CONFLICT)
        # address_type has a server default the INSERT does not read back.
        await db.refresh(address)

        logger.info("Created address with ID: %s for user %s", address.id, current_user.id)


        created = await db.get(Address, address.id)
        logger.info("Verified address exists: %s for user %s", created and created.id, current_user.id)

        set_location(request, response, "get_address", address_id=address.id)
        return AddressResponse.model_validate(address)
    except ValidationError as e:
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.VALIDATION_ERROR,
            detail="Invalid address data",
            errors=sanitized_errors(e.errors()),
        ) from None


_UPDATE_CONFLICTS = (
    "Address is not active (it was deleted); or the change makes it the same as another active "
    "address of the current user (street, city, state, postal code and country); or another "
    "request made a different address the default at the same time"
)


# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    summary="Patch Address",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("AddressUpdate"),
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": _UPDATE_CONFLICTS},
    },
)
@router.put(
    "/addresses/{address_id:sint}",
    response_model=AddressResponse,
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": _UPDATE_CONFLICTS},
    },
)
async def update_address(
    address_id: Int32,
    address_in: AddressUpdate,
    current_user: CurrentUser,
    db: DbSession,
):
    """Update fields on one of the current user's addresses. Sending
    `is_default` true makes it the default and unsets the previous one;
    `is_default` false on the default leaves the user with no default (no
    other address is promoted). An order keeps the address it was placed
    with: a later change does not alter it."""

    await _lock_user_addresses(db, current_user.id)
    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info("Found address %s for update request from user %s", address_id, current_user.id)

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )

    if not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {address_id} is not active"
        )




    if address_in.is_default:
        existing_default = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active,
                Address.is_default,
                Address.id != address_id
            ).limit(1)
        )).scalars().first()
        if existing_default:
            existing_default.is_default = False
            # Written before this address becomes the default: the flush
            # orders the UPDATEs by id, and uix_user_default_address checks
            # each row as it is written.
            await db.flush()


    for field, value in address_in.model_dump(exclude_unset=True).items():
        setattr(address, field, value)

    await _commit_address_change(db, ADDRESS_CONFLICT)
    await db.refresh(address)

    return AddressResponse.model_validate(address)

@router.delete(
    "/addresses/{address_id:sint}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        404: {"description": "Address not found, or already deleted"},
        409: {"description": "It was the default, and another request made a different address the "
                             "default at the same time; nothing was deleted"},
    },
)
async def delete_address(
    address_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """Deactivate one of the current user's addresses.

    A deactivated address is gone for the client, so deleting it again is a
    404 like any other address that does not exist. Orders already placed
    with it keep it. If it was the default, the most recently created
    remaining address becomes the default."""

    await _lock_user_addresses(db, current_user.id)
    address = (await db.execute(
        select(Address).where(
            Address.id == address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info("Processing deletion request for address %s from user %s", address_id, current_user.id)

    if not address or not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {address_id} not found"
        )


    address.is_active = False


    if address.is_default:
        # Written before another address becomes the default (see update_address).
        await db.flush()
        new_default = (await db.execute(
            select(Address).where(
                Address.user_id == current_user.id,
                Address.is_active,
                Address.id != address_id
            ).order_by(Address.created_at.desc()).limit(1)
        )).scalars().first()
        if new_default:
            new_default.is_default = True

    await _commit_address_change(db, DEFAULT_CONFLICT)

@router.post(
    "/addresses/default",
    response_model=Message,
    responses={
        404: {"description": "Address not found (or not the current user's)"},
        409: {"description": "Address is not active (it was deleted), or another request made a "
                             "different address the default at the same time"},
    },
)
async def set_default_address(
    default_address: SetDefaultAddress,
    current_user: CurrentUser,
    db: DbSession,
):
    """Mark one of the current user's addresses as the default shipping address."""

    await _lock_user_addresses(db, current_user.id)
    address = (await db.execute(
        select(Address).where(
            Address.id == default_address.address_id,
            Address.user_id == current_user.id
        ).limit(1)
    )).scalars().first()

    logger.info("Processing set default request for address %s from user %s", default_address.address_id, current_user.id)

    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Address {default_address.address_id} not found"
        )

    if not address.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Address {default_address.address_id} is not active"
        )


    # The others first: uix_user_default_address checks each row as it is
    # written, and this address's change is only written by the commit.
    await db.execute(
        update(Address)
        .where(
            Address.user_id == current_user.id,
            Address.is_active,
            Address.id != address.id
        )
        .values(is_default=False)
        .execution_options(synchronize_session=False)
    )
    address.is_default = True

    await _commit_address_change(db, DEFAULT_CONFLICT)

    return Message(message="Default address updated successfully")
