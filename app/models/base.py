from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import MetaData, Numeric
from sqlalchemy.orm import DeclarativeBase

from app.core.clock import UTCDateTime

# Constraint naming convention (SQLAlchemy "Configuring Constraint Naming
# Conventions", Alembic "The Importance of Naming Constraints"): every
# constraint and index gets a deterministic name, so a migration can drop or
# alter it by that name instead of by a name the database generated.
# - ix and fk follow the documented templates; uq the documented one too.
# - pk is PostgreSQL's own default (<table>_pkey), so no primary key was renamed.
# - There is no "ck" template: every CHECK constraint is named explicitly
#   (ck_order_status, ...) and keeps exactly that name.
# Indexes and unique constraints that already carry an explicit name
# (idx_*, uix_*, uq_users_email_lower) keep it as well.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Every datetime column of the schema is the aware-UTC UTCDateTime and
    # every Decimal column is a money amount, Numeric(10, 2); a column that
    # needs anything else passes its type to mapped_column() explicitly.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        Decimal: Numeric(10, 2),
    }
