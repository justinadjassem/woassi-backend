"""FastAPI dependencies of the catalog api."""

from typing import Annotated, cast

from fastapi import Depends, Request

from woassi.modules.catalog.application.facade import CatalogUseCases


def get_catalog(request: Request) -> CatalogUseCases:
    """The catalog use cases, provided by the `Container` stored on the app."""
    return cast(CatalogUseCases, request.app.state.container)


CatalogDep = Annotated[CatalogUseCases, Depends(get_catalog)]
