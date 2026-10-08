"""Offset pagination shared by every list endpoint."""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class PageParams(BaseModel):
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
