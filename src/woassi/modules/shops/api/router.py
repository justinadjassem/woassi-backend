"""Shop routes.

`account_router` holds the routes of the logged-in account (`/shops`); `router` holds the
routes of one shop (`/shops/{shop_id}`), which check that the account owns it.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from woassi.modules.shops.api.deps import ShopsDep
from woassi.modules.shops.api.schemas import (
    LogoConfirm,
    LogoUploadRequest,
    LogoUploadResponse,
    ReceivingAccountBody,
    ReceivingAccountResponse,
    SettingsBody,
    ShopCreate,
    ShopResponse,
    ShopUpdate,
    WhatsAppConnect,
    WhatsAppResponse,
)
from woassi.shared.api.deps import CurrentAccount, client_ip, get_authorized_shop
from woassi.shared.api.pagination import Limit, Offset, Page

account_router = APIRouter(prefix="/shops", tags=["shops"])
router = APIRouter(
    prefix="/shops/{shop_id}",
    tags=["shops"],
    dependencies=[Depends(get_authorized_shop)],
)


@account_router.post("", status_code=status.HTTP_201_CREATED)
async def create_shop(body: ShopCreate, account: CurrentAccount, shops: ShopsDep) -> ShopResponse:
    """Creates a shop for the current account, in DRAFT until a number is connected."""
    shop = await shops.create_shop().execute(account_id=account.id, **body.model_dump())
    return ShopResponse.from_view(shop)


@account_router.get("")
async def list_shops(
    account: CurrentAccount, shops: ShopsDep, offset: Offset = 0, limit: Limit = 50
) -> Page[ShopResponse]:
    """Lists the current account's shops (archived ones excluded)."""
    result = await shops.list_shops().execute(account_id=account.id, offset=offset, limit=limit)
    return Page[ShopResponse].from_result(result, ShopResponse.from_view)


@router.get("")
async def get_shop(shop_id: UUID, shops: ShopsDep) -> ShopResponse:
    """One shop."""
    return ShopResponse.from_view(await shops.get_shop().execute(shop_id))


@router.patch("")
async def update_shop(shop_id: UUID, body: ShopUpdate, shops: ShopsDep) -> ShopResponse:
    """Updates the shop's profile. Fields left out are unchanged; the slug never changes."""
    shop = await shops.update_shop().execute(shop_id=shop_id, **body.model_dump(exclude_unset=True))
    return ShopResponse.from_view(shop)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def archive_shop(shop_id: UUID, shops: ShopsDep) -> None:
    """Archives the shop and frees its WhatsApp number. Every route then answers 404."""
    await shops.archive_shop().execute(shop_id)


@router.get("/settings")
async def get_settings(shop_id: UUID, shops: ShopsDep) -> SettingsBody:
    """The shop's settings."""
    return SettingsBody.from_view(await shops.get_settings().execute(shop_id))


@router.put("/settings")
async def replace_settings(shop_id: UUID, body: SettingsBody, shops: ShopsDep) -> SettingsBody:
    """Replaces every setting at once (fields left out get their default)."""
    settings = await shops.replace_settings().execute(
        shop_id=shop_id,
        **body.to_arguments(),  # type: ignore[arg-type]
    )
    return SettingsBody.from_view(settings)


@router.post("/whatsapp/connect")
async def connect_whatsapp(
    shop_id: UUID, body: WhatsAppConnect, shops: ShopsDep
) -> WhatsAppResponse:
    """Finishes Embedded Signup: exchanges the code and connects the number."""
    account = await shops.connect_whatsapp().execute(shop_id=shop_id, **body.model_dump())
    return WhatsAppResponse.from_view(account)


@router.get("/whatsapp")
async def get_whatsapp(shop_id: UUID, shops: ShopsDep) -> WhatsAppResponse:
    """The shop's WhatsApp connection, without its token."""
    return WhatsAppResponse.from_view(await shops.get_whatsapp().execute(shop_id))


@router.delete("/whatsapp", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_whatsapp(shop_id: UUID, shops: ShopsDep) -> None:
    """Disconnects the number, which sends the shop back to DRAFT."""
    await shops.disconnect_whatsapp().execute(shop_id)


@router.get("/receiving-account")
async def get_receiving_account(shop_id: UUID, shops: ShopsDep) -> ReceivingAccountResponse | None:
    """The account the shop's customers pay into, or null."""
    account = await shops.get_receiving_account().execute(shop_id)
    return ReceivingAccountResponse.from_view(account) if account else None


@router.put("/receiving-account")
async def set_receiving_account(
    shop_id: UUID,
    body: ReceivingAccountBody,
    request: Request,
    account: CurrentAccount,
    shops: ShopsDep,
) -> ReceivingAccountResponse:
    """Sets the receiving account. Changing operator or number resets `verified`."""
    saved = await shops.set_receiving_account().execute(
        shop_id=shop_id, account_id=account.id, ip=client_ip(request), **body.model_dump()
    )
    return ReceivingAccountResponse.from_view(saved)


@router.delete("/receiving-account", status_code=status.HTTP_204_NO_CONTENT)
async def delete_receiving_account(
    shop_id: UUID, request: Request, account: CurrentAccount, shops: ShopsDep
) -> None:
    """Removes the receiving account."""
    await shops.delete_receiving_account().execute(
        shop_id=shop_id, account_id=account.id, ip=client_ip(request)
    )


@router.post("/logo/upload-url")
async def request_logo_upload(
    shop_id: UUID, body: LogoUploadRequest, shops: ShopsDep
) -> LogoUploadResponse:
    """Returns a presigned URL to PUT a new logo to."""
    upload = await shops.request_logo_upload().execute(
        shop_id=shop_id, content_type=body.content_type, size=body.size
    )
    return LogoUploadResponse.from_upload(upload)


@router.post("/logo/confirm")
async def confirm_logo(shop_id: UUID, body: LogoConfirm, shops: ShopsDep) -> ShopResponse:
    """Converts the uploaded logo to WebP and makes it the shop's logo."""
    shop = await shops.confirm_logo().execute(shop_id=shop_id, logo_id=body.logo_id)
    return ShopResponse.from_view(shop)
