"""Graph API calls of the Embedded Signup flow.

Checked on 2026-11-06 against Meta's documentation, for Graph API v27.0.

Secrets travel in query strings: see GraphClient, which keeps them out of logs and exceptions.
"""

from typing import Any

import structlog

from woassi.modules.shops.application.ports import MetaError, MetaFailure, MetaPhoneNumber
from woassi.shared.infrastructure.meta_graph import GraphClient, GraphUnavailable

log = structlog.get_logger()

INVALID_TOKEN_CODE = 190
CLOUD_API_PLATFORM = "CLOUD_API"


class HttpxMetaOnboarding:
    """`MetaOnboardingPort` on the Graph API."""

    def __init__(self, graph: GraphClient, *, app_id: str, app_secret: str) -> None:
        self._graph = graph
        self._app_id = app_id
        self._app_secret = app_secret

    async def exchange_code(self, code: str) -> str:
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-solution-partner
        """Raises MetaError(INVALID_CODE) if Meta refuses the code."""
        data = await self._request(
            "GET",
            "/oauth/access_token",
            params={
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "code": code,
            },
            invalid_request=MetaFailure.INVALID_CODE,
        )
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise MetaError(MetaFailure.REJECTED)
        return token

    async def subscribe_app(self, waba_id: str, token: str) -> None:
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/manage-webhooks/
        await self._request("POST", f"/{waba_id}/subscribed_apps", token=token)

    async def get_phone_number(self, phone_number_id: str, token: str) -> MetaPhoneNumber:
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/manage-phone-numbers
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/
        data = await self._request(
            "GET",
            f"/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name,is_on_biz_app,platform_type"},
            token=token,
        )
        return MetaPhoneNumber(
            display_phone_number=str(data.get("display_phone_number", "")),
            verified_name=str(data.get("verified_name", "")),
            on_business_app=data.get("is_on_biz_app") is True,
            on_cloud_api=data.get("platform_type") == CLOUD_API_PLATFORM,
        )

    async def link_catalog(
        self, waba_id: str, phone_number_id: str, token: str, catalog_id: str | None
    ) -> str | None:
        # https://developers.facebook.com/docs/graph-api/reference/whatsapp-business-account/product_catalogs/
        """Returns the account's catalog, linking `catalog_id` first if it has none.

        With a catalog, also shows it and enables the cart on the number.
        """
        data = await self._request("GET", f"/{waba_id}/product_catalogs", token=token)
        catalogs = data.get("data")
        linked = catalogs[0].get("id") if isinstance(catalogs, list) and catalogs else None
        if linked is None and catalog_id is not None:
            await self._request(
                "POST",
                f"/{waba_id}/product_catalogs",
                params={"catalog_id": catalog_id},
                token=token,
            )
            linked = catalog_id
        if linked is None:
            return None

        # https://developers.facebook.com/documentation/business-messaging/whatsapp/catalogs/set-commerce-settings
        await self._request(
            "POST",
            f"/{phone_number_id}/whatsapp_commerce_settings",
            params={"is_catalog_visible": "true", "is_cart_enabled": "true"},
            token=token,
        )
        return str(linked)

    async def sync_business_app_data(self, phone_number_id: str, token: str) -> None:
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/
        """Requests the contacts sync, then the history sync."""
        for sync_type in ("smb_app_state_sync", "history"):
            data = await self._request(
                "POST",
                f"/{phone_number_id}/smb_app_data",
                json={"messaging_product": "whatsapp", "sync_type": sync_type},
                token=token,
            )
            # Meta asks to keep this id for support.
            log.info(
                "whatsapp_sync_requested", sync_type=sync_type, request_id=data.get("request_id")
            )

    async def unsubscribe_app(self, waba_id: str, token: str) -> None:
        # https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/manage-webhooks/
        await self._request("DELETE", f"/{waba_id}/subscribed_apps", token=token)

    async def token_is_valid(self, token: str) -> bool:
        # https://developers.facebook.com/docs/graph-api/reference/debug_token/
        """Asks `debug_token`, authenticated with the app token."""
        data = await self._request(
            "GET",
            "/debug_token",
            params={"input_token": token, "access_token": f"{self._app_id}|{self._app_secret}"},
        )
        details = data.get("data")
        return isinstance(details, dict) and details.get("is_valid") is True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
        token: str | None = None,
        invalid_request: MetaFailure = MetaFailure.REJECTED,
    ) -> dict[str, Any]:
        """Sends a Graph request and returns its JSON body.

        Raises MetaError: UNAVAILABLE for no answer or a 5xx, INVALID_TOKEN for code 190,
        otherwise
        `invalid_request`.
        """
        try:
            response = await self._graph.request(
                method, path, params=params, json=json, token=token
            )
        except GraphUnavailable:
            raise MetaError(MetaFailure.UNAVAILABLE) from None

        if response.ok:
            return response.data
        meta_code = response.error_code
        if response.status >= 500:
            raise MetaError(MetaFailure.UNAVAILABLE, meta_code=meta_code)
        if meta_code == INVALID_TOKEN_CODE and invalid_request is MetaFailure.REJECTED:
            raise MetaError(MetaFailure.INVALID_TOKEN, meta_code=meta_code)
        raise MetaError(invalid_request, meta_code=meta_code)
