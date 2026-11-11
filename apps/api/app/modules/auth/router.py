import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, CurrentUser, SessionDep, require_roles
from app.core.config import settings
from app.core.security import (
    TokenConfigurationError,
    create_token,
    decode_token,
    hash_password,
    verify_password,
    verify_unknown_user_password,
)
from app.db.models import (
    AuthRefreshSession,
    Customer,
    CustomerEmailVerification,
    MembershipRole,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    User,
)
from app.modules.auth.schemas import (
    CreateMembershipRequest,
    CustomerLoginRequest,
    CustomerRegistrationRequest,
    CustomerVerificationRequest,
    CustomerVerificationResponse,
    MembershipResponse,
    OrganizationResponse,
    RefreshRequest,
    RegisterRequest,
    TokenRequest,
    TokenResponse,
    UserResponse,
)
from app.modules.customer.email import email_configuration_errors, send_verification_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["authentication"])
AdminUser = Annotated[AuthenticatedUser, Depends(require_roles(MembershipRole.ADMIN))]
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


async def _token_response(
    user: User,
    organization: Organization,
    role: MembershipRole,
    session: AsyncSession,
) -> TokenResponse:
    try:
        access_token = create_token(user.id, organization.id, role, "access")
        refresh_token = create_token(user.id, organization.id, role, "refresh")
    except TokenConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from exc
    refresh_claims = decode_token(refresh_token, expected_type="refresh")
    session.add(
        AuthRefreshSession(
            jti=UUID(refresh_claims["jti"]),
            user_id=user.id,
            organization_id=organization.id,
            expires_at=datetime.fromtimestamp(refresh_claims["exp"], UTC),
        )
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserResponse(id=user.id, email=user.email, full_name=user.full_name),
        organization=OrganizationResponse(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
        ),
        role=role,
    )


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _valid_slug(slug: str) -> bool:
    return bool(_SLUG_PATTERN.fullmatch(slug))


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: SessionDep) -> TokenResponse:
    if not settings.allow_self_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled",
        )
    if not _valid_slug(payload.organization_slug):
        raise HTTPException(status_code=422, detail="Invalid organization slug")

    email = _normalize_email(str(payload.email))
    existing_user = await session.scalar(select(User.id).where(User.email == email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered"
        )
    existing_organization = await session.scalar(
        select(Organization.id).where(Organization.slug == payload.organization_slug)
    )
    if existing_organization is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization slug is already registered",
        )

    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
    )
    organization = Organization(
        name=payload.organization_name.strip(), slug=payload.organization_slug
    )
    membership = OrganizationMembership(
        organization=organization,
        user=user,
        role=MembershipRole.ADMIN.value,
    )
    session.add_all([user, organization, membership])
    try:
        await session.flush()
        response = await _token_response(user, organization, MembershipRole.ADMIN, session)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.info("Registration conflict", extra={"email_domain": email.rpartition("@")[2]})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email or organization slug is already registered",
        ) from exc
    await session.refresh(user)
    await session.refresh(organization)
    return response


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: TokenRequest, session: SessionDep) -> TokenResponse:
    email = _normalize_email(str(payload.email))
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        verify_unknown_user_password(payload.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )
    password_is_valid = verify_password(payload.password, user.password_hash)
    if not user.is_active or not password_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )

    statement = (
        select(OrganizationMembership, Organization)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.status == MembershipStatus.ACTIVE.value,
            OrganizationMembership.role != MembershipRole.CUSTOMER.value,
            Organization.is_active.is_(True),
        )
    )
    if payload.organization_id is not None:
        statement = statement.where(Organization.id == payload.organization_id)
    memberships = (await session.execute(statement)).all()
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.organization_id is None and len(memberships) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id is required for users with multiple organizations",
        )
    membership, organization = memberships[0]
    response = await _token_response(user, organization, MembershipRole(membership.role), session)
    await session.commit()
    return response


async def _refresh(
    payload: RefreshRequest,
    session: AsyncSession,
    expected_role: MembershipRole | None,
) -> TokenResponse:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = UUID(claims["sub"])
    organization_id = UUID(claims["org"])
    refresh_session = await session.scalar(
        select(AuthRefreshSession)
        .where(AuthRefreshSession.jti == UUID(claims["jti"]))
        .with_for_update()
    )
    now = datetime.now(UTC)
    expires_at = None if refresh_session is None else refresh_session.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        refresh_session is None
        or refresh_session.revoked_at is not None
        or expires_at <= now
        or refresh_session.user_id != user_id
        or refresh_session.organization_id != organization_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user, organization, organization_membership = membership
    try:
        role = MembershipRole(organization_membership.role)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if (
        (expected_role is not None and role != expected_role)
        or (expected_role is None and role == MembershipRole.CUSTOMER)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    refresh_session.revoked_at = now
    response = await _token_response(user, organization, role, session)
    await session.commit()
    return response


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenResponse:
    return await _refresh(payload, session, expected_role=None)


def _generic_customer_registration_response() -> dict[str, str]:
    return {
        "message": (
            "If the email matches an eligible customer profile, "
            "a verification link will be sent."
        )
    }


@router.post("/customer/auth/register", status_code=status.HTTP_202_ACCEPTED)
async def register_customer(
    payload: CustomerRegistrationRequest,
    session: SessionDep,
) -> dict[str, str]:
    configuration_errors = email_configuration_errors()
    if configuration_errors:
        logger.error(
            "Customer verification email is misconfigured",
            extra={"configuration_errors": configuration_errors},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Customer email verification is not configured",
        )

    email = _normalize_email(str(payload.email))
    organization = await session.scalar(
        select(Organization).where(
            Organization.slug == payload.organization_slug,
            Organization.is_active.is_(True),
        )
    )
    if organization is None:
        return _generic_customer_registration_response()
    customers = list(
        await session.scalars(
            select(Customer)
            .where(
                Customer.organization_id == organization.id,
                func.lower(Customer.email) == email,
            )
            .with_for_update()
            .limit(2)
        )
    )
    if len(customers) != 1:
        return _generic_customer_registration_response()
    customer = customers[0]

    user = await session.scalar(select(User).where(User.email == email).with_for_update())
    if user is not None:
        non_customer_membership = await session.scalar(
            select(OrganizationMembership.id).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.role != MembershipRole.CUSTOMER.value,
            )
        )
        if non_customer_membership is not None or not user.is_active:
            return _generic_customer_registration_response()

    membership = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == organization.id,
            OrganizationMembership.customer_id == customer.id,
        )
        .with_for_update()
    )
    if membership is not None and (
        membership.role != MembershipRole.CUSTOMER.value
        or membership.status != MembershipStatus.PENDING.value
        or (user is not None and membership.user_id != user.id)
        or user is None
    ):
        return _generic_customer_registration_response()
    if user is None:
        user = User(
            email=email,
            full_name=customer.full_name,
            password_hash=hash_password(payload.password),
        )
        session.add(user)
        await session.flush()
    elif membership is None and await session.scalar(
        select(OrganizationMembership.id).where(
            OrganizationMembership.organization_id == organization.id,
            OrganizationMembership.user_id == user.id,
        )
    ):
        return _generic_customer_registration_response()

    cooldown_start = datetime.now(UTC) - timedelta(
        seconds=settings.customer_email_resend_cooldown_seconds
    )
    recent_verification = await session.scalar(
        select(CustomerEmailVerification.id).where(
            CustomerEmailVerification.organization_id == organization.id,
            CustomerEmailVerification.user_id == user.id,
            CustomerEmailVerification.created_at >= cooldown_start,
        )
    )
    if recent_verification is not None:
        return _generic_customer_registration_response()

    if membership is None:
        membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=user.id,
            customer_id=customer.id,
            role=MembershipRole.CUSTOMER.value,
            status=MembershipStatus.PENDING.value,
        )
        session.add(membership)
        await session.flush()
    previous_tokens = await session.scalars(
        select(CustomerEmailVerification).where(
            CustomerEmailVerification.organization_id == organization.id,
            CustomerEmailVerification.user_id == user.id,
            CustomerEmailVerification.used_at.is_(None),
        )
    )
    for previous_token in previous_tokens:
        await session.delete(previous_token)

    raw_token = secrets.token_urlsafe(32)
    session.add(
        CustomerEmailVerification(
            organization_id=organization.id,
            user_id=user.id,
            customer_id=customer.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC)
            + timedelta(minutes=settings.customer_email_verification_minutes),
        )
    )
    await session.commit()

    try:
        await send_verification_email(email, organization.name, raw_token)
    except (OSError, RuntimeError) as exc:
        logger.error(
            "Unable to deliver customer verification email",
            extra={"organization_id": str(organization.id), "error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to send verification email. Please try again later.",
        ) from exc
    return _generic_customer_registration_response()


@router.post(
    "/customer/auth/verify",
    response_model=CustomerVerificationResponse,
)
async def verify_customer(
    payload: CustomerVerificationRequest,
    session: SessionDep,
) -> CustomerVerificationResponse:
    now = datetime.now(UTC)
    token = await session.scalar(
        select(CustomerEmailVerification)
        .where(
            CustomerEmailVerification.token_hash
            == hashlib.sha256(payload.token.encode()).hexdigest(),
            CustomerEmailVerification.used_at.is_(None),
        )
        .with_for_update()
    )
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token",
        )
    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token",
        )
    membership = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == token.organization_id,
            OrganizationMembership.user_id == token.user_id,
            OrganizationMembership.customer_id == token.customer_id,
            OrganizationMembership.role == MembershipRole.CUSTOMER.value,
            OrganizationMembership.status == MembershipStatus.PENDING.value,
        )
        .with_for_update()
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token",
        )
    matching_email = await session.scalar(
        select(User.id)
        .join(Customer, Customer.id == token.customer_id)
        .where(
            User.id == token.user_id,
            Customer.organization_id == token.organization_id,
            func.lower(Customer.email) == User.email,
        )
    )
    if matching_email is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token",
        )
    membership.status = MembershipStatus.ACTIVE.value
    token.used_at = now
    await session.commit()
    return CustomerVerificationResponse(message="Your email is verified. You can now sign in.")


@router.post("/customer/auth/login", response_model=TokenResponse)
async def login_customer(
    payload: CustomerLoginRequest,
    session: SessionDep,
) -> TokenResponse:
    email = _normalize_email(str(payload.email))
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        verify_unknown_user_password(payload.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect, or email has not been verified",
            headers={"WWW-Authenticate": "Bearer"},
        )
    password_is_valid = verify_password(payload.password, user.password_hash)
    organization = await session.scalar(
        select(Organization).where(
            Organization.slug == payload.organization_slug,
            Organization.is_active.is_(True),
        )
    )
    membership = None
    if organization is not None:
        membership = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization.id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.role == MembershipRole.CUSTOMER.value,
                OrganizationMembership.status == MembershipStatus.ACTIVE.value,
            )
        )
    if not user.is_active or not password_is_valid or membership is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect, or email has not been verified",
            headers={"WWW-Authenticate": "Bearer"},
        )
    response = await _token_response(user, organization, MembershipRole.CUSTOMER, session)
    await session.commit()
    return response


@router.post("/customer/auth/refresh", response_model=TokenResponse)
async def refresh_customer(payload: RefreshRequest, session: SessionDep) -> TokenResponse:
    return await _refresh(payload, session, expected_role=MembershipRole.CUSTOMER)


@router.get("/auth/me", response_model=MembershipResponse)
async def me(user: CurrentUser) -> MembershipResponse:
    return MembershipResponse(
        user=UserResponse(id=user.user_id, email=user.email, full_name=user.full_name),
        organization=OrganizationResponse(
            id=user.organization_id,
            name=user.organization_name,
            slug=user.organization_slug,
        ),
        role=user.role,
        status=MembershipStatus.ACTIVE.value,
    )


@router.get("/organizations", response_model=list[OrganizationResponse])
async def list_organizations(user: CurrentUser, session: SessionDep) -> list[OrganizationResponse]:
    organizations = await session.scalars(
        select(Organization)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(
            OrganizationMembership.user_id == user.user_id,
            OrganizationMembership.status == MembershipStatus.ACTIVE.value,
            Organization.is_active.is_(True),
        )
        .order_by(Organization.name)
    )
    return [OrganizationResponse.model_validate(organization) for organization in organizations]


@router.get(
    "/organizations/{organization_id}/memberships",
    response_model=list[MembershipResponse],
)
async def list_memberships(
    organization_id: UUID,
    user: AdminUser,
    session: SessionDep,
) -> list[MembershipResponse]:
    if organization_id != user.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    rows = await session.execute(
        select(User, Organization, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(Organization.id == user.organization_id)
        .order_by(User.email)
    )
    return [
        MembershipResponse(
            user=UserResponse(id=member.id, email=member.email, full_name=member.full_name),
            organization=OrganizationResponse(
                id=organization.id,
                name=organization.name,
                slug=organization.slug,
            ),
            role=MembershipRole(membership.role),
            status=MembershipStatus(membership.status),
        )
        for member, organization, membership in rows
    ]


@router.post(
    "/organizations/{organization_id}/memberships",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_membership(
    organization_id: UUID,
    payload: CreateMembershipRequest,
    user: AdminUser,
    session: SessionDep,
) -> MembershipResponse:
    if organization_id != user.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    email = _normalize_email(str(payload.email))
    existing_user = await session.scalar(select(User.id).where(User.email == email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        )
    organization = await session.get(Organization, user.organization_id)
    if organization is None or not organization.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    new_user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
    )
    membership = OrganizationMembership(
        organization=organization,
        user=new_user,
        role=payload.role,
    )
    session.add_all([new_user, membership])
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.info(
            "Membership creation conflict", extra={"organization_id": str(user.organization_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        ) from exc
    await session.refresh(new_user)
    return MembershipResponse(
        user=UserResponse(id=new_user.id, email=new_user.email, full_name=new_user.full_name),
        organization=OrganizationResponse(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
        ),
        role=MembershipRole(payload.role),
        status=MembershipStatus.ACTIVE.value,
    )
