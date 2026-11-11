import asyncio
from typing import Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings


class KnowledgeStorageError(RuntimeError):
    pass


class KnowledgeObjectStorage(Protocol):
    async def put(self, object_key: str, content: bytes, _content_type: str) -> None: ...

    async def get(self, object_key: str) -> bytes: ...

    async def delete(self, object_key: str) -> None: ...

    async def check_health(self) -> None: ...


class S3KnowledgeObjectStorage:
    def __init__(self) -> None:
        use_aws_s3 = settings.knowledge_storage_backend == "aws_s3"
        secret_access_key = settings.s3_secret_access_key
        config_options = (
            {}
            if use_aws_s3
            else {"s3": {"addressing_style": "path"}}
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=(
                None
                if use_aws_s3
                else settings.s3_endpoint_url or "http://localhost:9000"
            ),
            aws_access_key_id=None if use_aws_s3 else settings.s3_access_key_id,
            aws_secret_access_key=(
                None
                if use_aws_s3 or secret_access_key is None
                else secret_access_key.get_secret_value()
            ),
            region_name=settings.s3_region_name,
            config=Config(signature_version="s3v4", **config_options),
        )

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=settings.knowledge_bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if settings.knowledge_storage_backend == "aws_s3":
                raise KnowledgeStorageError("object_storage_bucket_unavailable") from exc
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise KnowledgeStorageError("object_storage_unavailable") from exc
            try:
                self._client.create_bucket(Bucket=settings.knowledge_bucket)
            except ClientError as create_exc:
                create_code = create_exc.response.get("Error", {}).get("Code")
                if create_code != "BucketAlreadyOwnedByYou":
                    raise KnowledgeStorageError("object_storage_unavailable") from create_exc
        except BotoCoreError as exc:
            raise KnowledgeStorageError("object_storage_unavailable") from exc

    def _check_health(self) -> None:
        self._ensure_bucket()

    async def check_health(self) -> None:
        await asyncio.to_thread(self._check_health)

    async def put(self, object_key: str, content: bytes, content_type: str) -> None:
        await asyncio.to_thread(self._put, object_key, content, content_type)

    def _put(self, object_key: str, content: bytes, content_type: str) -> None:
        self._ensure_bucket()
        try:
            self._client.put_object(
                Bucket=settings.knowledge_bucket,
                Key=object_key,
                Body=content,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise KnowledgeStorageError("object_storage_write_failed") from exc

    async def get(self, object_key: str) -> bytes:
        return await asyncio.to_thread(self._get, object_key)

    def _get(self, object_key: str) -> bytes:
        self._ensure_bucket()
        try:
            response = self._client.get_object(Bucket=settings.knowledge_bucket, Key=object_key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise KnowledgeStorageError("object_storage_read_failed") from exc

    async def delete(self, object_key: str) -> None:
        await asyncio.to_thread(self._delete, object_key)

    def _delete(self, object_key: str) -> None:
        self._ensure_bucket()
        try:
            self._client.delete_object(Bucket=settings.knowledge_bucket, Key=object_key)
        except (BotoCoreError, ClientError) as exc:
            raise KnowledgeStorageError("object_storage_delete_failed") from exc
