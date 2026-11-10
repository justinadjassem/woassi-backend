"""Shared pagination for list routes: `Offset`, `Limit` and the `Page` response."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from woassi.shared.application.pagination import PageResult

Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


class Page[T](BaseModel):
    """One page of items, and the total number of matching items."""

    items: list[T]
    total: int
    offset: int
    limit: int

    @classmethod
    def from_result[V](cls, result: PageResult[V], convert: Callable[[V], T]) -> "Page[T]":
        """Builds a page from a use case's `PageResult`, converting each item with `convert`."""
        return cls(
            items=[convert(item) for item in result.items],
            total=result.total,
            offset=result.offset,
            limit=result.limit,
        )
