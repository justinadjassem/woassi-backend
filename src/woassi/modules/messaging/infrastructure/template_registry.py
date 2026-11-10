"""Template management through the Graph API.

https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-management.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/utility-templates/utility-templates.md
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/components.md
"""

from typing import Any

from woassi.modules.messaging.application.ports import (
    SendError,
    SendFailure,
    ShopConnection,
    SubmittedTemplate,
)
from woassi.modules.messaging.domain.templates import TemplateDefinition, same_language
from woassi.shared.infrastructure.meta_graph import GraphClient, GraphUnavailable

# "Content in This Language Already Exists": the template was submitted before.
ALREADY_EXISTS_SUBCODE = 2388024


def _create_body(definition: TemplateDefinition) -> dict[str, Any]:
    """Graph API body creating a UTILITY template with named parameters."""
    components: list[dict[str, Any]] = [
        {
            "type": "BODY",
            "text": definition.body,
            "example": {
                "body_text_named_params": [
                    {"param_name": name, "example": value}
                    for name, value in definition.examples.items()
                ]
            },
        }
    ]
    if definition.quick_replies:
        components.append(
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": text} for text in definition.quick_replies
                ],
            }
        )
    return {
        "name": definition.name,
        "language": definition.language,
        "category": "UTILITY",
        "parameter_format": "NAMED",
        "components": components,
    }


class GraphTemplateRegistry:
    """`TemplateRegistry` on the Graph API."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def submit(
        self, connection: ShopConnection, definition: TemplateDefinition
    ) -> SubmittedTemplate:
        """Submits the template; if Meta already has it, returns its current status instead.

        Raises SendError if Meta cannot be reached or refuses it.
        """
        try:
            response = await self._graph.request(
                "POST",
                f"/{connection.waba_id}/message_templates",
                json=_create_body(definition),
                token=connection.access_token,
            )
        except GraphUnavailable:
            raise SendError(SendFailure.UNAVAILABLE) from None
        if response.ok:
            template_id = response.data.get("id")
            return SubmittedTemplate(
                meta_template_id=str(template_id) if template_id else None,
                status=str(response.data.get("status", "PENDING")),
            )
        error = response.data.get("error")
        subcode = error.get("error_subcode") if isinstance(error, dict) else None
        if subcode == ALREADY_EXISTS_SUBCODE:
            existing = await self.status(connection, definition.name, definition.language)
            if existing is not None:
                return existing
        raise SendError(SendFailure.REJECTED, meta_code=response.error_code)

    async def status(
        self, connection: ShopConnection, name: str, language: str
    ) -> SubmittedTemplate | None:
        """Looks the template up by name and language. Raises SendError on failure."""
        try:
            response = await self._graph.request(
                "GET",
                f"/{connection.waba_id}/message_templates",
                params={"name": name, "fields": "id,name,language,status"},
                token=connection.access_token,
            )
        except GraphUnavailable:
            raise SendError(SendFailure.UNAVAILABLE) from None
        if not response.ok:
            raise SendError(SendFailure.REJECTED, meta_code=response.error_code)
        for item in response.data.get("data") or []:
            if (
                isinstance(item, dict)
                and item.get("name") == name
                and same_language(str(item.get("language", "")), language)
            ):
                template_id = item.get("id")
                return SubmittedTemplate(
                    meta_template_id=str(template_id) if template_id else None,
                    status=str(item.get("status", "")),
                )
        return None
