"""Use cases the shops api needs, provided by the composition root."""

from typing import Protocol

from woassi.modules.shops.application.use_cases.logo import ConfirmLogo, RequestLogoUpload
from woassi.modules.shops.application.use_cases.receiving_account import (
    DeleteReceivingAccount,
    GetReceivingAccount,
    SetReceivingAccount,
)
from woassi.modules.shops.application.use_cases.settings import GetSettings, ReplaceSettings
from woassi.modules.shops.application.use_cases.shops import (
    ArchiveShop,
    CreateShop,
    GetShop,
    ListShops,
    UpdateShop,
)
from woassi.modules.shops.application.use_cases.whatsapp import (
    ConnectWhatsApp,
    DisconnectWhatsApp,
    GetWhatsApp,
)


class ShopsUseCases(Protocol):
    """What this module expects from the composition root (see catalog.application.facade)."""

    def create_shop(self) -> CreateShop: ...

    def list_shops(self) -> ListShops: ...

    def get_shop(self) -> GetShop: ...

    def update_shop(self) -> UpdateShop: ...

    def archive_shop(self) -> ArchiveShop: ...

    def get_settings(self) -> GetSettings: ...

    def replace_settings(self) -> ReplaceSettings: ...

    def connect_whatsapp(self) -> ConnectWhatsApp: ...

    def get_whatsapp(self) -> GetWhatsApp: ...

    def disconnect_whatsapp(self) -> DisconnectWhatsApp: ...

    def get_receiving_account(self) -> GetReceivingAccount: ...

    def set_receiving_account(self) -> SetReceivingAccount: ...

    def delete_receiving_account(self) -> DeleteReceivingAccount: ...

    def request_logo_upload(self) -> RequestLogoUpload: ...

    def confirm_logo(self) -> ConfirmLogo: ...
