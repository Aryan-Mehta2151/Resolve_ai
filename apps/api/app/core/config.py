from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ResolveAI API"
    environment: str = "local"
    database_url: SecretStr = SecretStr("postgresql+asyncpg://localhost:5432/resolveai")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    celery_result_backend: SecretStr = SecretStr("redis://localhost:6379/1")
    s3_endpoint_url: str | None = "http://localhost:9000"
    healthcheck_timeout_seconds: float = 2.0
    jwt_secret_key: SecretStr | None = None
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    allow_self_registration: bool = False
    demo_business_workflows_enabled: bool = False
    vapi_enabled: bool = False
    vapi_public_key: str = ""
    vapi_assistant_id: str = ""
    vapi_webhook_secret: SecretStr | None = None
    vapi_public_api_url: str = ""
    vapi_result_wait_seconds: float = Field(default=8.0, ge=0, le=15)
    vapi_reachability_check_enabled: bool = True
    knowledge_storage_backend: Literal["minio", "aws_s3"] = "minio"
    s3_access_key_id: str | None = "resolveai"
    s3_secret_access_key: SecretStr | None = SecretStr("local-development-only-change-me")
    s3_region_name: str = "us-east-1"
    knowledge_bucket: str = "resolveai-knowledge"
    knowledge_max_file_bytes: int = Field(default=10_485_760, ge=1_048_576, le=52_428_800)
    knowledge_ingestion_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    agent_intent_provider: Literal["deterministic", "openai"] = "deterministic"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    agent_model_timeout_seconds: float = Field(default=8.0, gt=0, le=120)
    customer_email_smtp_host: str | None = None
    customer_email_smtp_port: int = Field(default=1025, ge=1, le=65535)
    customer_email_smtp_username: str | None = None
    customer_email_smtp_password: SecretStr | None = None
    customer_email_smtp_starttls: bool = True
    customer_email_from_address: str = "no-reply@localhost"
    customer_email_verification_url: str = "http://localhost:3000/customer"
    customer_email_verification_minutes: int = Field(default=30, ge=5, le=1440)
    customer_email_resend_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    voice_verification_minutes: int = Field(default=5, ge=1, le=10)
    voice_verification_resend_cooldown_seconds: int = Field(default=60, ge=30, le=3600)


settings = Settings()
