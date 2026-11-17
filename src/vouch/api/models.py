from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[
    str,
    StringConstraints(min_length=1, strip_whitespace=True)
]

# ================ Web Investigation Request Models =============

class WebInvestigationRequest(BaseModel):
    event_id: NonEmptyString
    message: str = Field(min_length=1, max_length=10000)
    attachments: list[str] = Field(default_factory=list)

# ================ WhatsApp Webhook Models =====================

class ProviderPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class WhatsAppMessage(ProviderPayload):
    id: NonEmptyString
    type: str


class WhatsAppStatus(ProviderPayload):
    id: NonEmptyString
    status: str


class WhatsAppMetadata(ProviderPayload):
    display_phone_number: str | None = None
    phone_number_id: str | None = None


class WhatsAppValue(ProviderPayload):
    messaging_product: Literal["whatsapp"]
    metadata: WhatsAppMetadata | None = None

    messages: list[WhatsAppMessage] = Field(default_factory=list)
    statuses: list[WhatsAppStatus] = Field(default_factory=list)


class WhatsAppChange(ProviderPayload):
    field: Literal["messages"]
    value: WhatsAppValue


class WhatsAppEntry(ProviderPayload):
    id: NonEmptyString
    changes: list[WhatsAppChange] = Field(min_length=1)


class WhatsAppWebhookRequest(ProviderPayload):
    object: Literal["whatsapp_business_account"]
    entry: list[WhatsAppEntry] = Field(min_length=1)

# ================ Telegram Webhook Models =====================

class TelegramWebhookRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        strict=True,
    )

    update_id: int = Field(gt=0)
