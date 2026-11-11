from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import settings


def register(
    client: TestClient,
    *,
    email: str = "admin@example.com",
    slug: str = "resolveai-demo",
) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "ResolveAI Demo",
            "organization_slug": slug,
            "full_name": "Demo Admin",
            "email": email,
            "password": "Correct-Horse-Battery-1!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def test_register_creates_tenant_admin_and_hashed_password(client: TestClient) -> None:
    response = register(client, email="Admin@Example.com")

    assert response["user"]["email"] == "admin@example.com"
    assert response["organization"]["slug"] == "resolveai-demo"
    assert response["role"] == "admin"
    assert response["token_type"] == "bearer"
    assert response["expires_in"] == 900

    current_user = client.get("/api/v1/auth/me", headers=auth_headers(response["access_token"]))
    assert current_user.status_code == 200
    assert current_user.json()["user"]["full_name"] == "Demo Admin"
    assert current_user.json()["role"] == "admin"

    organizations = client.get(
        "/api/v1/organizations", headers=auth_headers(response["access_token"])
    )
    assert [organization["slug"] for organization in organizations.json()] == ["resolveai-demo"]


def test_login_is_case_insensitive_and_refresh_reissues_tokens(client: TestClient) -> None:
    register(client)

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "ADMIN@example.com", "password": "Correct-Horse-Battery-1!"},
    )
    assert login_response.status_code == 200
    refresh_response = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login_response.json()["refresh_token"]},
    )

    assert refresh_response.status_code == 200
    assert refresh_response.json()["user"]["email"] == "admin@example.com"
    refreshed_access = client.get(
        "/api/v1/auth/me",
        headers=auth_headers(refresh_response.json()["access_token"]),
    )
    assert refreshed_access.status_code == 200
    rotated_refresh = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_response.json()["refresh_token"]},
    )
    assert rotated_refresh.status_code == 200
    replayed = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login_response.json()["refresh_token"]},
    )
    assert replayed.status_code == 401


def test_invalid_credentials_and_access_token_rejected(client: TestClient) -> None:
    register(client)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "Incorrect-password-123"},
    )
    assert response.status_code == 401

    refresh_token = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "Correct-Horse-Battery-1!"},
    ).json()["refresh_token"]
    refresh_as_access = client.get("/api/v1/auth/me", headers=auth_headers(refresh_token))
    assert refresh_as_access.status_code == 401


def test_duplicate_email_and_organization_slug_are_rejected(client: TestClient) -> None:
    tokens = register(client)
    duplicate_email = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Another Organization",
            "organization_slug": "another-organization",
            "full_name": "Another Admin",
            "email": "ADMIN@example.com",
            "password": "Another-Correct-Password-1!",
        },
    )
    duplicate_slug = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Another Organization",
            "organization_slug": "resolveai-demo",
            "full_name": "Another Admin",
            "email": "another@example.com",
            "password": "Another-Correct-Password-1!",
        },
    )

    assert duplicate_email.status_code == 409
    assert duplicate_slug.status_code == 409
    members = client.get(
        f"/api/v1/organizations/{tokens['organization']['id']}/memberships",
        headers=auth_headers(tokens["access_token"]),
    )
    assert len(members.json()) == 1


def test_admin_can_create_member_but_viewer_cannot_manage_memberships(client: TestClient) -> None:
    admin = register(client)
    organization_id = admin["organization"]["id"]
    create_response = client.post(
        f"/api/v1/organizations/{organization_id}/memberships",
        headers=auth_headers(admin["access_token"]),
        json={
            "full_name": "Support Viewer",
            "email": "viewer@example.com",
            "password": "Viewer-Correct-Password-1!",
            "role": "viewer",
        },
    )
    assert create_response.status_code == 201
    assert create_response.json()["role"] == "viewer"

    viewer_login = client.post(
        "/api/v1/auth/login",
        json={"email": "viewer@example.com", "password": "Viewer-Correct-Password-1!"},
    )
    assert viewer_login.status_code == 200
    denied = client.get(
        f"/api/v1/organizations/{organization_id}/memberships",
        headers=auth_headers(viewer_login.json()["access_token"]),
    )
    assert denied.status_code == 403


def test_tenant_access_is_scoped_to_token_organization(client: TestClient) -> None:
    admin_a = register(client)
    admin_b = register(client, email="other@example.com", slug="other-organization")

    cross_tenant = client.get(
        f"/api/v1/organizations/{admin_b['organization']['id']}/memberships",
        headers=auth_headers(admin_a["access_token"]),
    )

    assert cross_tenant.status_code == 404
    assert UUID(admin_a["organization"]["id"]) != UUID(admin_b["organization"]["id"])


def test_registration_requires_configuration_and_valid_password(client: TestClient) -> None:
    weak_password = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "ResolveAI Demo",
            "organization_slug": "resolveai-demo",
            "full_name": "Demo Admin",
            "email": "admin@example.com",
            "password": "short",
        },
    )
    malformed_slug = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "ResolveAI Demo",
            "organization_slug": "not a valid slug",
            "full_name": "Demo Admin",
            "email": "admin@example.com",
            "password": "Correct-Horse-Battery-1!",
        },
    )

    assert weak_password.status_code == 422
    assert malformed_slug.status_code == 422


def test_registration_requires_explicit_opt_in_and_signing_key(
    client: TestClient,
    monkeypatch,
) -> None:
    payload = {
        "organization_name": "ResolveAI Demo",
        "organization_slug": "resolveai-demo",
        "full_name": "Demo Admin",
        "email": "admin@example.com",
        "password": "Correct-Horse-Battery-1!",
    }
    monkeypatch.setattr(settings, "allow_self_registration", False)
    registration_disabled = client.post("/api/v1/auth/register", json=payload)
    assert registration_disabled.status_code == 403

    monkeypatch.setattr(settings, "allow_self_registration", True)
    monkeypatch.setattr(settings, "jwt_secret_key", SecretStr(""))
    missing_signing_key = client.post("/api/v1/auth/register", json=payload)
    assert missing_signing_key.status_code == 503
