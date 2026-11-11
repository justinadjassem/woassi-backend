import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from woassi.modules.catalog.application.ports import (
    ITEM_REJECTED,
    MetaCatalogAccess,
    MetaCatalogError,
    MetaCatalogFailure,
    MetaCatalogItem,
)
from woassi.modules.catalog.infrastructure.meta_catalog import GraphMetaCatalog
from woassi.shared.infrastructure.meta_graph import GraphClient

TOKEN = "EAAG-a-recognizable-business-token"
ACCESS = MetaCatalogAccess(catalog_id="CAT-1", access_token=TOKEN, brand="Chez Ama")
ITEM = MetaCatalogItem(
    retailer_id="SKU-1",
    title="Pagne",
    description="Wax 6 yards",
    price=5000,
    in_stock=False,
    image_urls=("https://img/a_1024.webp", "https://img/b_1024.webp"),
    link="https://woassi.test/shops/s/products/p",
    brand="Chez Ama",
)
ACCEPTED = {"handles": ["AcxHandle"], "validation_status": []}


class Meta:
    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)  # type: ignore[no-any-return]


def catalog(meta: Meta) -> GraphMetaCatalog:
    client = httpx.AsyncClient(transport=httpx.MockTransport(meta.handler))
    return GraphMetaCatalog(GraphClient(client, graph_version="v27.0"))


async def test_items_batch_request() -> None:
    meta = Meta(lambda _: httpx.Response(200, json=ACCEPTED))

    assert await catalog(meta).write(ACCESS, [ITEM], ["SKU-OLD"]) == {}

    [request] = meta.requests
    assert request.method == "POST"
    assert str(request.url) == "https://graph.facebook.com/v27.0/CAT-1/items_batch"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert meta.body == {
        "item_type": "PRODUCT_ITEM",
        "allow_upsert": True,
        "requests": [
            {
                "method": "UPDATE",
                "data": {
                    "id": "SKU-1",
                    "title": "Pagne",
                    "description": "Wax 6 yards",
                    "availability": "out of stock",
                    "condition": "new",
                    "price": "5000 XOF",
                    "link": "https://woassi.test/shops/s/products/p",
                    "image": [
                        {"url": "https://img/a_1024.webp"},
                        {"url": "https://img/b_1024.webp"},
                    ],
                    "brand": "Chez Ama",
                },
            },
            {"method": "DELETE", "data": {"id": "SKU-OLD"}},
        ],
    }


async def test_items_with_errors_are_rejected_alone_warnings_do_not_count() -> None:
    answer = {
        "handles": ["h"],
        "validation_status": [
            {"retailer_id": "SKU-1", "errors": [{"message": "Meta's text"}], "warnings": []},
            {"retailer_id": "SKU-2", "errors": [], "warnings": [{"message": "w"}]},
        ],
    }
    meta = Meta(lambda _: httpx.Response(200, json=answer))

    assert await catalog(meta).write(ACCESS, [ITEM], []) == {"SKU-1": ITEM_REJECTED}


@pytest.mark.parametrize(
    ("response", "failure", "meta_code"),
    [
        (httpx.Response(500, json={"error": {"code": 2}}), MetaCatalogFailure.UNAVAILABLE, 2),
        (
            httpx.Response(400, json={"error": {"code": 80014}}),
            MetaCatalogFailure.RATE_LIMITED,
            80014,
        ),
        (httpx.Response(401, json={"error": {"code": 190}}), MetaCatalogFailure.INVALID_TOKEN, 190),
        (httpx.Response(400, json={"error": {"code": 100}}), MetaCatalogFailure.REJECTED, 100),
        (httpx.Response(200, json={"handles": []}), MetaCatalogFailure.REJECTED, None),
    ],
)
async def test_batch_failures(
    response: httpx.Response, failure: MetaCatalogFailure, meta_code: int | None
) -> None:
    meta = Meta(lambda _: response)

    with pytest.raises(MetaCatalogError) as caught:
        await catalog(meta).write(ACCESS, [ITEM], [])

    assert (caught.value.failure, caught.value.meta_code) == (failure, meta_code)


async def test_unreachable_meta_leaks_neither_url_nor_token() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(MetaCatalogError) as caught:
        await catalog(Meta(refuse)).write(ACCESS, [ITEM], [])

    assert caught.value.failure is MetaCatalogFailure.UNAVAILABLE
    assert caught.value.__cause__ is None
    assert TOKEN not in repr(caught.value)
    assert "CAT-1" not in str(caught.value)
    assert TOKEN not in repr(ACCESS)
