import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenConfigurationError, decode_token
from app.db.models import (
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    User,
)
from app.db.session import get_session

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)
SessionDep = Annotated[AsyncSession, Depends(get_session)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str
    full_name: str
    organization_id: UUID
    organization_name: str
    organization_slug: str
    role: MembershipRole
    customer_id: UUID | None


async def get_current_user(
    credentials: CredentialsDep,
    session: SessionDep,
) -> AuthenticatedUser:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        claims = decode_token(credentials.credentials, expected_type="access")
        user_id = UUID(claims["sub"])
        organization_id = UUID(claims["org"])
    except (jwt.InvalidTokenError, TokenConfigurationError, ValueError) as exc:
        if isinstance(exc, TokenConfigurationError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is not configured",
            ) from exc
        raise unauthorized from exc

    result = await session.execute(
        select(User, Organization, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            User.id == user_id,
            User.is_active.is_(True),
            Organization.id == organization_id,
            Organization.is_active.is_(True),
            OrganizationMembership.status == MembershipStatus.ACTIVE.value,
        )
    )
    membership = result.one_or_none()
    if membership is None:
        raise unauthorized
    user, organization, organization_membership = membership
    try:
        role = MembershipRole(organization_membership.role)
    except ValueError as exc:
        logger.error(
            "Stored membership role is invalid",
            extra={
                "organization_id": str(organization.id),
                "membership_id": str(organization_membership.id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Membership role is invalid",
        ) from exc
    return AuthenticatedUser(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization_id=organization.id,
        organization_name=organization.name,
        organization_slug=organization.slug,
        role=role,
        customer_id=organization_membership.customer_id,
    )


AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_staff_user(user: AuthenticatedUserDep) -> AuthenticatedUser:
    if user.role == MembershipRole.CUSTOMER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Customer accounts cannot access staff workspace APIs",
        )
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_staff_user)]


def require_roles(*allowed_roles: MembershipRole) -> Callable[..., AuthenticatedUser]:
    async def check_role(user: AuthenticatedUserDep) -> AuthenticatedUser:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return check_role
