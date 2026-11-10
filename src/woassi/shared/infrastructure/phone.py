"""Phone number parsing with the `phonenumbers` library."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from woassi.shared.domain.errors import InvalidData
from woassi.shared.domain.values import PhoneNumber


class PhonenumbersNormalizer:
    """Parses free-form phone numbers; numbers without a country code use `default_region`."""

    def __init__(self, default_region: str = "TG") -> None:
        self._default_region = default_region

    def normalize(self, raw: str) -> PhoneNumber:
        """Returns `raw` in E.164, or raises InvalidData if it is not a valid number."""
        try:
            parsed = phonenumbers.parse(raw, self._default_region)
        except NumberParseException as error:
            raise InvalidData("Invalid phone number.", code="phone.invalid") from error
        if not phonenumbers.is_valid_number(parsed):
            raise InvalidData("Invalid phone number.", code="phone.invalid")
        return PhoneNumber(phonenumbers.format_number(parsed, PhoneNumberFormat.E164))
