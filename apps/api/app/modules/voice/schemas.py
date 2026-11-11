from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.phone import normalize_e164_phone_number


class CallCreate(BaseModel):
    customer_id: UUID | None = None
    consent: Literal[True]


class CustomerCallCreate(BaseModel):
    consent: Literal[True]


class CallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    provider_call_id: str | None
    channel: str
    status: str
    ended_reason: str | None
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None


class CallStarted(BaseModel):
    call: CallResponse
    public_key: str
    assistant_id: str
    session_token: str


class FunctionCall(BaseModel):
    name: str = Field(max_length=128)
    arguments: dict | str = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    function: FunctionCall


class ProviderCall(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    assistantId: str
    type: Literal["webCall", "inboundPhoneCall"]
    phoneNumberId: str | None = Field(default=None, max_length=128)
    customer: dict = Field(default_factory=dict)
    assistantOverrides: dict = Field(default_factory=dict)


class VoicePhoneNumberCreate(BaseModel):
    provider_number_id: str = Field(min_length=1, max_length=128)
    phone_number: str = Field(min_length=9, max_length=32)
    service_user_id: UUID

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        try:
            number = normalize_e164_phone_number(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if number is None:
            raise ValueError("Phone number is required")
        return number


class VoicePhoneNumberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider_number_id: str
    phone_number: str
    service_user_id: UUID
    is_active: bool
    created_at: datetime


class VapiMessage(BaseModel):
    type: str
    call: ProviderCall
    status: str | None = None
    endedReason: str | None = Field(default=None, max_length=160)
    toolCallList: list[ToolCall] = Field(default_factory=list, max_length=5)
    artifact: dict = Field(default_factory=dict)


class VapiWebhook(BaseModel):
    message: VapiMessage
