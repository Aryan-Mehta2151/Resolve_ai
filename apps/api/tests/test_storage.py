import asyncio

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.core.config import settings
from app.modules.knowledge.storage import (
    KnowledgeStorageError,
    S3KnowledgeObjectStorage,
)


class MissingBucketClient:
    def __init__(self) -> None:
        self.created_buckets: list[str] = []

    def head_bucket(self, *, Bucket: str) -> None:
        raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, *, Bucket: str) -> None:
        self.created_buckets.append(Bucket)


def test_aws_s3_uses_default_credentials_and_does_not_override_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = MissingBucketClient()

    def create_client(service: str, **kwargs: object) -> MissingBucketClient:
        captured["service"] = service
        captured.update(kwargs)
        return client

    monkeypatch.setattr(settings, "knowledge_storage_backend", "aws_s3")
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://minio:9000")
    monkeypatch.setattr(settings, "s3_access_key_id", "local-access-key")
    monkeypatch.setattr(settings, "s3_secret_access_key", SecretStr("local-secret"))
    monkeypatch.setattr(settings, "s3_region_name", "us-west-2")
    monkeypatch.setattr(settings, "knowledge_bucket", "resolveai-demo")
    monkeypatch.setattr("app.modules.knowledge.storage.boto3.client", create_client)

    storage = S3KnowledgeObjectStorage()

    assert captured["service"] == "s3"
    assert captured["endpoint_url"] is None
    assert captured["aws_access_key_id"] is None
    assert captured["aws_secret_access_key"] is None
    assert captured["region_name"] == "us-west-2"

    with pytest.raises(KnowledgeStorageError, match="object_storage_bucket_unavailable"):
        asyncio.run(storage.check_health())
    assert client.created_buckets == []


def test_minio_creates_a_missing_local_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MissingBucketClient()

    def create_client(service: str, **kwargs: object) -> MissingBucketClient:
        assert service == "s3"
        assert kwargs["endpoint_url"] == "http://minio:9000"
        return client

    monkeypatch.setattr(settings, "knowledge_storage_backend", "minio")
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://minio:9000")
    monkeypatch.setattr(settings, "knowledge_bucket", "resolveai-knowledge")
    monkeypatch.setattr("app.modules.knowledge.storage.boto3.client", create_client)

    storage = S3KnowledgeObjectStorage()
    asyncio.run(storage.check_health())

    assert client.created_buckets == ["resolveai-knowledge"]
