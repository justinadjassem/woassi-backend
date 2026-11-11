"""Writes products to a Meta catalog through the Graph API (Catalog Batch API).

Checked on 2026-11-10 against Meta's documentation:
- https://developers.facebook.com/docs/marketing-api/reference/product-catalog/items_batch/
- https://developers.facebook.com/docs/marketing-api/catalog/reference/ (item fields)
- https://developers.facebook.com/docs/marketing-api/reference/product-catalog/check_batch_request_status/

Every call goes through GraphClient, which keeps the token and URLs out of logs and
exceptions. Meta's error messages are never stored nor logged: only our codes and Meta's
numeric code.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from woassi.modules.catalog.application.ports import (
    ITEM_REJECTED,
    MetaCatalogAccess,
    MetaCatalogError,
    MetaCatalogFailure,
    MetaCatalogItem,
)
from woassi.shared.infrastructure.meta_graph import GraphClient, GraphUnavailable

CURRENCY = "XOF"
INVALID_TOKEN_CODE = 190
# 80014: too many batch calls to this catalog; 4: application request limit.
RATE_LIMIT_CODES = frozenset({4, 80014})


def _price(amount: int) -> str:
    """`<amount> <ISO 4217 code>`, as Meta documents it ("9.99 USD").

    XOF has no minor unit, so the amount is a whole number: "5000 XOF".
    """
    return f"{amount} {CURRENCY}"


def _upsert(item: MetaCatalogItem) -> dict[str, Any]:
    """An UPDATE request; with `allow_upsert` it creates the item if Meta does not have it.

    Sends every field required for a PRODUCT_ITEM, so the upsert is a valid creation too.
    """
    return {
        "method": "UPDATE",
        "data": {
            "id": item.retailer_id,
            "title": item.title[:200],
            "description": item.description[:9999],
            "availability": "in stock" if item.in_stock else "out of stock",
            "condition": "new",
            "price": _price(item.price),
            "link": item.link,
            "image": [{"url": url} for url in item.image_urls],
            "brand": item.brand[:100],
        },
    }


def _delete(retailer_id: str) -> dict[str, Any]:
    """A DELETE request: only the identifying field."""
    return {"method": "DELETE", "data": {"id": retailer_id}}


def _rejected(data: Mapping[str, Any]) -> dict[str, str]:
    """Retailer ids that `validation_status` reports with errors (warnings do not count)."""
    rejected: dict[str, str] = {}
    for status in data.get("validation_status") or []:
        if not isinstance(status, Mapping):
            continue
        retailer_id = status.get("retailer_id")
        if isinstance(retailer_id, str) and status.get("errors"):
            rejected[retailer_id] = ITEM_REJECTED
    return rejected


class GraphMetaCatalog:
    """`MetaCatalogPort` on the Graph API."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def write(
        self,
        access: MetaCatalogAccess,
        upserts: Sequence[MetaCatalogItem],
        deletes: Sequence[str],
    ) -> Mapping[str, str]:
        # https://developers.facebook.com/docs/marketing-api/reference/product-catalog/items_batch/
        """One `items_batch` call. Upserts are idempotent, so a timeout can be retried.

        Meta ingests the batch asynchronously; its `handle` could be checked with
        `GET /{catalog_id}/check_batch_request_status?handle=...`, which we do not do yet:
        only the synchronous `validation_status` is read.
        """
        requests = [_upsert(item) for item in upserts] + [_delete(rid) for rid in deletes]
        try:
            response = await self._graph.request(
                "POST",
                f"/{access.catalog_id}/items_batch",
                json={"item_type": "PRODUCT_ITEM", "allow_upsert": True, "requests": requests},
                token=access.access_token,
            )
        except GraphUnavailable:
            raise MetaCatalogError(MetaCatalogFailure.UNAVAILABLE) from None

        meta_code = response.error_code
        if response.status >= 500:
            raise MetaCatalogError(MetaCatalogFailure.UNAVAILABLE, meta_code=meta_code)
        if not response.ok:
            if meta_code in RATE_LIMIT_CODES:
                raise MetaCatalogError(MetaCatalogFailure.RATE_LIMITED, meta_code=meta_code)
            if meta_code == INVALID_TOKEN_CODE:
                raise MetaCatalogError(MetaCatalogFailure.INVALID_TOKEN, meta_code=meta_code)
            raise MetaCatalogError(MetaCatalogFailure.REJECTED, meta_code=meta_code)

        handles = response.data.get("handles")
        if not isinstance(handles, list) or not handles:
            # "An empty list means nothing was ingested."
            raise MetaCatalogError(MetaCatalogFailure.REJECTED)
        return _rejected(response.data)
