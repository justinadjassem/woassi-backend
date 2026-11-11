"""Use cases the catalog api and worker need, provided by the composition root."""

from typing import Protocol

from woassi.modules.catalog.application.use_cases.meta_sync import (
    RequestCatalogResync,
    RetryCatalogSync,
    SyncCatalogToMeta,
)
from woassi.modules.catalog.application.use_cases.photos import (
    ConfirmPhoto,
    ProcessPhoto,
    RequestPhotoUpload,
)
from woassi.modules.catalog.application.use_cases.products import (
    CreateProduct,
    GetProduct,
    ListProducts,
    UpdateProduct,
)


class CatalogUseCases(Protocol):
    """What this module expects from the composition root.

    The api cannot import woassi.container: it imports every module, which would break their
    independence. Container inherits from this Protocol, so mypy checks that it complies.
    """

    def create_product(self) -> CreateProduct: ...

    def list_products(self) -> ListProducts: ...

    def get_product(self) -> GetProduct: ...

    def update_product(self) -> UpdateProduct: ...

    def request_catalog_resync(self) -> RequestCatalogResync: ...

    def sync_catalog_to_meta(self) -> SyncCatalogToMeta: ...

    def retry_catalog_sync(self) -> RetryCatalogSync: ...

    def request_photo_upload(self) -> RequestPhotoUpload: ...

    def confirm_photo(self) -> ConfirmPhoto: ...

    def process_photo(self) -> ProcessPhoto: ...
