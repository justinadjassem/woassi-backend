"""The mobile money account that receives a shop's payments. Every change is audited."""

from uuid import UUID

from woassi.modules.shops.application.dto import ReceivingAccountView, receiving_account_view
from woassi.modules.shops.application.ports import ShopsUnitOfWork
from woassi.modules.shops.application.use_cases.support import load_shop
from woassi.modules.shops.domain.errors import InvalidReceivingAccount
from woassi.modules.shops.domain.shop import Operator, ReceivingAccount
from woassi.shared.application.ports import PhoneNormalizer, SecretCipher
from woassi.shared.domain.values import PhoneNumber

TARGET_TYPE = "receiving_account"


def _masked(number: PhoneNumber) -> str:
    """The audit log tells that the payout number changed, without spreading it."""
    return f"{number.value[:4]}****{number.value[-2:]}"


class GetReceivingAccount:
    """Returns the shop's receiving account, if any."""

    def __init__(self, uow: ShopsUnitOfWork, cipher: SecretCipher) -> None:
        self._uow = uow
        self._cipher = cipher

    async def execute(self, shop_id: UUID) -> ReceivingAccountView | None:
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
        account = shop.receiving_account
        return receiving_account_view(account, self._cipher) if account else None


class SetReceivingAccount:
    """Creates or replaces the shop's receiving account."""

    def __init__(self, uow: ShopsUnitOfWork, phones: PhoneNormalizer, cipher: SecretCipher) -> None:
        self._uow = uow
        self._phones = phones
        self._cipher = cipher

    async def execute(
        self,
        *,
        shop_id: UUID,
        account_id: UUID,
        operator: Operator,
        number: str,
        holder_name: str,
        aggregator_merchant_id: str | None = None,
        ip: str | None = None,
    ) -> ReceivingAccountView:
        """Raises InvalidData for an invalid number, InvalidReceivingAccount for a blank holder."""
        normalized = self._phones.normalize(number)
        if not holder_name.strip():
            raise InvalidReceivingAccount("The holder name is required.")
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            previous = shop.receiving_account
            # This number receives the money: a new one has to be verified again.
            same_payee = (
                previous is not None
                and previous.operator is operator
                and previous.number == normalized
            )
            account = ReceivingAccount(
                operator=operator,
                number=normalized,
                holder_name=holder_name.strip(),
                encrypted_merchant_id=(
                    self._cipher.encrypt(aggregator_merchant_id) if aggregator_merchant_id else None
                ),
                verified=same_payee and previous is not None and previous.verified,
            )
            shop.set_receiving_account(account)
            await uow.shops.save(shop)
            await uow.audit.record(
                account_id=account_id,
                action="receiving_account.updated",
                target_type=TARGET_TYPE,
                target_id=str(shop_id),
                shop_id=shop_id,
                details={"operator": operator.value, "number": _masked(normalized)},
                ip=ip,
            )
            await uow.commit()
        return receiving_account_view(account, self._cipher)


class DeleteReceivingAccount:
    """Removes the shop's receiving account. Does nothing if there is none."""

    def __init__(self, uow: ShopsUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, shop_id: UUID, account_id: UUID, ip: str | None = None) -> None:
        async with self._uow as uow:
            shop = await load_shop(uow, shop_id)
            if shop.receiving_account is None:
                return
            shop.set_receiving_account(None)
            await uow.shops.save(shop)
            await uow.audit.record(
                account_id=account_id,
                action="receiving_account.deleted",
                target_type=TARGET_TYPE,
                target_id=str(shop_id),
                shop_id=shop_id,
                ip=ip,
            )
            await uow.commit()
