"""Safe sorting for the paginated lists that take PaginationParams.

The client names a field in sort_by; the endpoint maps the public names it
allows to columns. Anything not in that map is never put near SQL: an unknown
sort_by (or none) gives the endpoint's default order. Falling back, instead of
answering 422, keeps a client that sends such a value working as it did when
the parameters were accepted and ignored.
"""
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement


def order_clauses(
    sort_by: str | None,
    sort_order: str | None,
    allowed: Mapping[str, ColumnElement[Any]],
    tie_breaker: ColumnElement[Any],
    default: Sequence[ColumnElement[Any]],
) -> list[ColumnElement[Any]]:
    """The ORDER BY terms of a list.

    A sort_by found in `allowed` sorts by that column, ascending unless
    sort_order is "desc", with `tie_breaker` (the primary key) last in the
    same direction so that LIMIT/OFFSET pages stay disjoint. Otherwise the
    `default` terms, which already end in the tie-breaker.
    """
    column = allowed.get(sort_by) if sort_by is not None else None
    if column is None:
        return list(default)
    descending = sort_order == "desc"
    terms = [column.desc() if descending else column.asc()]
    if column is not tie_breaker:
        terms.append(tie_breaker.desc() if descending else tie_breaker.asc())
    return terms
