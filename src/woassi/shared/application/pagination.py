"""Pagination result returned by list use cases."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageResult[T]:
    """One page of `items`, plus the `total` number of matching rows.

    The api turns it into `Page[T]`.
    """

    items: list[T]
    total: int
    offset: int
    limit: int
