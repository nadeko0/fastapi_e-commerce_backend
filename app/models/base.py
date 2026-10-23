from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import Numeric
from sqlalchemy.orm import DeclarativeBase

from app.core.clock import UTCDateTime


class Base(DeclarativeBase):
    # Every datetime column of the schema is the aware-UTC UTCDateTime and
    # every Decimal column is a money amount, Numeric(10, 2); a column that
    # needs anything else passes its type to mapped_column() explicitly.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        Decimal: Numeric(10, 2),
    }
