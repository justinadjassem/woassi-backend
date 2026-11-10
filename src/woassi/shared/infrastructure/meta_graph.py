"""Thin Graph API client shared by every module that talks to Meta.

Secrets travel in query strings where Meta documents them there, and access tokens in headers.
So nothing here logs a URL, and no httpx exception, which holds the URL, leaves this module.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from woassi.shared.infrastructure.logging import silence_url_logging

GRAPH_URL = "https://graph.facebook.com"


class GraphUnavailable(Exception):
    """No usable answer from Meta. Carries neither the URL nor Meta's text."""

    def __init__(self, *, request_sent: bool) -> None:
        super().__init__("Graph API unavailable")
        # False only when the connection failed: then Meta certainly did not act on the request.
        self.request_sent = request_sent


@dataclass(frozen=True, slots=True)
class GraphResponse:
    """Status code and JSON body of a Graph API answer, errors included."""

    status: int
    data: dict[str, Any]

    @property
    def ok(self) -> bool:
        """True for a 2xx answer."""
        return 200 <= self.status < 300

    @property
    def error_code(self) -> int | None:
        """Meta's numeric error code (`error.code`), if the body has one."""
        error = self.data.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, int) else None


class GraphClient:
    """Sends requests to the Graph API of one version, e.g. `v23.0`."""

    def __init__(self, client: httpx.AsyncClient, *, graph_version: str) -> None:
        silence_url_logging()
        self._client = client
        self._base_url = f"{GRAPH_URL}/{graph_version}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> GraphResponse:
        """Sends a request to `path` and returns Meta's answer, even an error one.

        `token` goes in the `Authorization` header. Raises GraphUnavailable when there is no usable
        answer: connection failure, timeout, or a body that is not JSON.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            response = await self._client.request(
                method, f"{self._base_url}{path}", params=params, json=json, headers=headers
            )
            data = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise GraphUnavailable(request_sent=False) from None
        except (httpx.HTTPError, ValueError):
            # `from None`: the original exception holds the URL, and so the secrets.
            raise GraphUnavailable(request_sent=True) from None
        return GraphResponse(response.status_code, data if isinstance(data, dict) else {})
