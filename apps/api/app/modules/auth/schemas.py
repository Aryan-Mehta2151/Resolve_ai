from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.db.models import MembershipRole

Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]
OrganizationSlug = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class TokenRequest(BaseModel):
    email: EmailStr
    password: Password
    organization_id: UUID | None = None


class RegisterRequest(BaseModel):
    organization_name: Annotated[str, Field(min_length=2, max_length=120)]
    organization_slug: OrganizationSlug
    full_name: FullName
    email: EmailStr
    password: Password


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class CreateMembershipRequest(BaseModel):
    full_name: FullName
    email: EmailStr
    password: Password
    role: Literal["admin", "supervisor", "support_agent", "viewer"] = "viewer"


class CustomerRegistrationRequest(BaseModel):
    organization_slug: OrganizationSlug
    email: EmailStr
    password: Password


class CustomerLoginRequest(BaseModel):
    organization_slug: OrganizationSlug
    email: EmailStr
    password: Password


class CustomerVerificationRequest(BaseModel):
    token: Annotated[str, StringConstraints(min_length=32, max_length=128)]


class CustomerVerificationResponse(BaseModel):
    message: str


class OrganizationResponse(BaseModel):
    id: UUID
    name: str
    slug: str

    model_config = ConfigDict(from_attributes=True)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str


class MembershipResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    role: MembershipRole
    status: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
    organization: OrganizationResponse
    role: MembershipRole
