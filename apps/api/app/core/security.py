from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import settings
from app.db.models import MembershipRole

password_hash = PasswordHash.recommended()
_DUMMY_PASSWORD_HASH = password_hash.hash("resolveai-invalid-user-password")
_JWT_ALGORITHM = "HS256"
_JWT_ISSUER = "resolveai"


class TokenConfigurationError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def verify_unknown_user_password(password: str) -> None:
    password_hash.verify(password, _DUMMY_PASSWORD_HASH)


def _signing_key() -> str:
    secret = settings.jwt_secret_key
    if secret is None or len(secret.get_secret_value()) < 32:
        raise TokenConfigurationError("JWT_SECRET_KEY must contain at least 32 characters")
    return secret.get_secret_value()


def create_token(
    user_id: UUID,
    organization_id: UUID,
    role: MembershipRole,
    token_type: str,
) -> str:
    now = datetime.now(UTC)
    if token_type == "access":
        expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    elif token_type == "refresh":
        expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    else:
        raise ValueError("Unsupported token type")

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(organization_id),
        "role": role.value,
        "type": token_type,
        "iss": _JWT_ISSUER,
        "iat": now,
        "exp": expires_at,
    }
    if token_type == "refresh":
        payload["jti"] = str(uuid4())
    return jwt.encode(payload, _signing_key(), algorithm=_JWT_ALGORITHM)


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    claims = jwt.decode(
        token,
        _signing_key(),
        algorithms=[_JWT_ALGORITHM],
        issuer=_JWT_ISSUER,
        options={
            "require": ["sub", "org", "type", "iss", "iat", "exp"]
            + (["jti"] if expected_type == "refresh" else [])
        },
    )
    if claims.get("type") != expected_type:
        raise jwt.InvalidTokenError("Unexpected token type")
    try:
        UUID(claims["sub"])
        UUID(claims["org"])
        if expected_type == "refresh":
            UUID(claims["jti"])
    except (TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("Invalid token subject") from exc
    return claims
