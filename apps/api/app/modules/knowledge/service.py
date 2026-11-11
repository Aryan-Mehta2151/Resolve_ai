import hashlib
import logging
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import AuthenticatedUser
from app.core.celery import KNOWLEDGE_DELETE_TASK_NAME, KNOWLEDGE_INGEST_TASK_NAME, celery_app
from app.core.config import settings
from app.db.models import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeStore,
    Organization,
)
from app.modules.knowledge.provider import (
    KnowledgeProvider,
    KnowledgeProviderError,
    OpenAIFileSearchProvider,
)
from app.modules.knowledge.schemas import KnowledgeAnswer, KnowledgeCitation
from app.modules.knowledge.storage import (
    KnowledgeObjectStorage,
    KnowledgeStorageError,
    S3KnowledgeObjectStorage,
)

logger = logging.getLogger(__name__)
NO_KNOWLEDGE_ANSWER = "I couldn't find that information in the current knowledge documents."
SUPPORTED_FILE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}
_UNSAFE_FILENAME = re.compile(r"[\x00-\x1f\x7f]")


class KnowledgeDocumentNotFound(LookupError):
    pass


class KnowledgeProviderNotConfigured(RuntimeError):
    pass


class KnowledgeJobDispatchError(RuntimeError):
    pass


def create_knowledge_provider() -> KnowledgeProvider | None:
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value().strip():
        return None
    return OpenAIFileSearchProvider(
        api_key=settings.openai_api_key.get_secret_value(),
        model=settings.openai_model,
        ingestion_timeout_seconds=settings.knowledge_ingestion_timeout_seconds,
    )


class KnowledgeService:
    def __init__(
        self,
        provider: KnowledgeProvider | None,
        storage: KnowledgeObjectStorage,
    ) -> None:
        self.provider = provider
        self.storage = storage

    async def aclose(self) -> None:
        if isinstance(self.provider, OpenAIFileSearchProvider):
            await self.provider.aclose()

    def require_provider(self) -> KnowledgeProvider:
        if self.provider is None:
            raise KnowledgeProviderNotConfigured
        return self.provider

    @staticmethod
    def validate_upload(
        filename: str | None, content_type: str | None, content: bytes
    ) -> tuple[str, str]:
        if filename is None:
            raise ValueError("A filename is required")
        safe_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not safe_filename or len(safe_filename) > 255 or _UNSAFE_FILENAME.search(safe_filename):
            raise ValueError("The filename is invalid")
        extension = "." + safe_filename.rpartition(".")[2].casefold()
        expected_type = SUPPORTED_FILE_TYPES.get(extension)
        if expected_type is None:
            raise ValueError("Only PDF, DOCX, Markdown, and text files are supported")
        if not content or len(content) > settings.knowledge_max_file_bytes:
            raise ValueError("The uploaded file is empty or exceeds the size limit")
        if content_type not in {None, "", "application/octet-stream", expected_type}:
            raise ValueError("The uploaded file type does not match its filename")
        if extension == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("The uploaded file is not a valid PDF")
        if extension == ".docx" and not content.startswith(b"PK\x03\x04"):
            raise ValueError("The uploaded file is not a valid DOCX")
        if extension in {".md", ".txt"}:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Text files must use UTF-8 encoding") from exc
            if b"\x00" in content:
                raise ValueError("Text files must not contain null bytes")
        return safe_filename, expected_type

    async def create_upload(
        self,
        session: AsyncSession,
        user: AuthenticatedUser,
        title: str,
        description: str | None,
        filename: str,
        content_type: str,
        content: bytes,
        document_id: UUID | None = None,
    ) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
        provider = self.require_provider()
        document = None
        if document_id is not None:
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.organization_id == user.organization_id,
                    KnowledgeDocument.status == "active",
                )
            )
            if document is None:
                raise KnowledgeDocumentNotFound

        new_document_id = document.id if document is not None else uuid4()
        version_id = uuid4()
        object_key = f"knowledge/{user.organization_id}/{new_document_id}/{version_id}/source"
        await self.storage.put(object_key, content, content_type)

        try:
            organization = await session.scalar(
                select(Organization)
                .where(Organization.id == user.organization_id)
                .with_for_update()
            )
            if organization is None:
                raise KnowledgeDocumentNotFound

            store = await session.scalar(
                select(KnowledgeStore)
                .where(KnowledgeStore.organization_id == user.organization_id)
                .with_for_update()
            )
            if store is None:
                store = KnowledgeStore(
                    organization_id=user.organization_id,
                    provider=provider.name,
                    status="pending",
                )
                session.add(store)
            elif store.provider != provider.name:
                raise KnowledgeProviderError("provider_configuration_changed")

            if document is None:
                document = KnowledgeDocument(
                    id=new_document_id,
                    organization_id=user.organization_id,
                    created_by_user_id=user.user_id,
                    title=title,
                    description=description,
                    status="active",
                )
                session.add(document)
                version_number = 1
            else:
                locked_document = await session.scalar(
                    select(KnowledgeDocument)
                    .where(
                        KnowledgeDocument.id == new_document_id,
                        KnowledgeDocument.organization_id == user.organization_id,
                        KnowledgeDocument.status == "active",
                    )
                    .with_for_update()
                )
                if locked_document is None:
                    raise KnowledgeDocumentNotFound
                document = locked_document
                version_number = (
                    await session.scalar(
                        select(func.max(KnowledgeDocumentVersion.version_number)).where(
                            KnowledgeDocumentVersion.organization_id == user.organization_id,
                            KnowledgeDocumentVersion.document_id == new_document_id,
                        )
                    )
                    or 0
                ) + 1

            version = KnowledgeDocumentVersion(
                id=version_id,
                organization_id=user.organization_id,
                document_id=new_document_id,
                version_number=version_number,
                original_filename=filename,
                content_type=content_type,
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                object_key=object_key,
                status="queued",
            )
            session.add(version)
            await session.commit()
            await session.refresh(document)
            await session.refresh(version)
        except SQLAlchemyError:
            await session.rollback()
            try:
                await self.storage.delete(object_key)
            except KnowledgeStorageError as cleanup_error:
                logger.error(
                    "Unable to clean up an unreferenced knowledge upload",
                    extra={"error_code": str(cleanup_error)},
                )
            raise
        except (KnowledgeDocumentNotFound, KnowledgeProviderError):
            await session.rollback()
            try:
                await self.storage.delete(object_key)
            except KnowledgeStorageError as cleanup_error:
                logger.error(
                    "Unable to clean up an unreferenced knowledge upload",
                    extra={"error_code": str(cleanup_error)},
                )
            raise
        return document, version

    async def enqueue_ingestion(
        self,
        session: AsyncSession,
        organization_id: UUID,
        document_id: UUID,
        version_id: UUID,
    ) -> None:
        try:
            celery_app.send_task(
                KNOWLEDGE_INGEST_TASK_NAME,
                args=[str(organization_id), str(document_id), str(version_id)],
            )
        except Exception as exc:
            version = await session.scalar(
                select(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.id == version_id,
                    KnowledgeDocumentVersion.organization_id == organization_id,
                )
            )
            if version is not None:
                version.status = "failed"
                version.error_code = "job_dispatch_failed"
                await session.commit()
            logger.error(
                "Unable to enqueue knowledge ingestion",
                extra={"version_id": str(version_id), "error_type": type(exc).__name__},
            )
            raise KnowledgeJobDispatchError from exc

    async def answer(
        self,
        session: AsyncSession,
        organization_id: UUID,
        question: str,
    ) -> KnowledgeAnswer | None:
        provider = self.require_provider()
        store = await session.scalar(
            select(KnowledgeStore).where(KnowledgeStore.organization_id == organization_id)
        )
        if store is None:
            return None
        if store.provider != provider.name:
            raise KnowledgeProviderError("provider_configuration_changed")
        if store.status == "failed":
            raise KnowledgeProviderError(store.error_code or "provider_store_failed")
        if store.status != "ready" or store.provider_store_id is None:
            return None
        ready_count = await session.scalar(
            select(func.count(KnowledgeDocumentVersion.id))
            .join(
                KnowledgeDocument,
                (KnowledgeDocument.organization_id == KnowledgeDocumentVersion.organization_id)
                & (KnowledgeDocument.id == KnowledgeDocumentVersion.document_id),
            )
            .where(
                KnowledgeDocumentVersion.organization_id == organization_id,
                KnowledgeDocumentVersion.status == "ready",
                KnowledgeDocument.status == "active",
            )
        )
        if not ready_count:
            return None

        provider_answer = await provider.answer(store.provider_store_id, question)
        file_ids = {citation.file_id for citation in provider_answer.citations}
        if not provider_answer.answer.strip() or not file_ids:
            return KnowledgeAnswer(answer=NO_KNOWLEDGE_ANSWER, citations=[])
        versions = await session.scalars(
            select(KnowledgeDocumentVersion)
            .join(
                KnowledgeDocument,
                (KnowledgeDocument.organization_id == KnowledgeDocumentVersion.organization_id)
                & (KnowledgeDocument.id == KnowledgeDocumentVersion.document_id),
            )
            .where(
                KnowledgeDocumentVersion.organization_id == organization_id,
                KnowledgeDocumentVersion.provider_file_id.in_(file_ids),
                KnowledgeDocumentVersion.status == "ready",
                KnowledgeDocument.status == "active",
            )
        )
        current_versions = {version.provider_file_id: version for version in versions}
        documents = await session.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.organization_id == organization_id,
                KnowledgeDocument.id.in_(
                    {version.document_id for version in current_versions.values()}
                ),
                KnowledgeDocument.status == "active",
            )
        )
        documents_by_id = {document.id: document for document in documents}
        citations = [
            KnowledgeCitation(
                document_id=version.document_id,
                version_id=version.id,
                title=documents_by_id[version.document_id].title,
                filename=version.original_filename,
                excerpt=provider_citation.excerpt,
            )
            for provider_citation in provider_answer.citations
            if (version := current_versions.get(provider_citation.file_id)) is not None
            and version.document_id in documents_by_id
        ]
        if not citations:
            return KnowledgeAnswer(answer=NO_KNOWLEDGE_ANSWER, citations=[])
        return KnowledgeAnswer(answer=provider_answer.answer.strip(), citations=citations)

    async def ensure_provider_store(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        organization_id: UUID,
    ) -> str:
        provider = self.require_provider()
        async with session_factory() as session:
            store = await session.scalar(
                select(KnowledgeStore)
                .where(KnowledgeStore.organization_id == organization_id)
                .with_for_update()
            )
            if store is None:
                raise KnowledgeProviderError("knowledge_store_missing")
            if store.provider != provider.name:
                raise KnowledgeProviderError("provider_configuration_changed")
            if store.provider_store_id is not None and store.status == "ready":
                return store.provider_store_id
            try:
                provider_store_id = await provider.create_store(organization_id)
            except KnowledgeProviderError as exc:
                store.status = "failed"
                store.error_code = exc.error_code
                await session.commit()
                raise
            store.provider_store_id = provider_store_id
            store.status = "ready"
            store.error_code = None
            await session.commit()
            return provider_store_id

    async def ingest_version(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        organization_id: UUID,
        document_id: UUID,
        version_id: UUID,
    ) -> None:
        provider = self.require_provider()
        async with session_factory() as session:
            version = await session.scalar(
                select(KnowledgeDocumentVersion)
                .where(
                    KnowledgeDocumentVersion.id == version_id,
                    KnowledgeDocumentVersion.document_id == document_id,
                    KnowledgeDocumentVersion.organization_id == organization_id,
                )
                .with_for_update()
            )
            document = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.organization_id == organization_id,
                    KnowledgeDocument.status == "active",
                )
            )
            if version is None or document is None:
                raise KnowledgeDocumentNotFound
            if version.status == "deleted":
                return
            if version.status in {"ready", "superseded"}:
                should_cleanup = True
            else:
                should_cleanup = False
                version.status = "processing"
                version.error_code = None
                await session.commit()
                object_key = version.object_key
                filename = version.original_filename

        if should_cleanup:
            await self._delete_superseded_files(
                session_factory,
                organization_id,
                document_id,
            )
            return

        try:
            provider_store_id = await self.ensure_provider_store(
                session_factory,
                organization_id,
            )
        except KnowledgeProviderError as exc:
            await self._mark_version_failed(
                session_factory,
                organization_id,
                version_id,
                exc.error_code,
            )
            raise
        try:
            content = await self.storage.get(object_key)
            provider_file_id = await provider.ingest_file(provider_store_id, filename, content)
        except (KnowledgeProviderError, KnowledgeStorageError) as exc:
            await self._mark_version_failed(
                session_factory,
                organization_id,
                version_id,
                exc.args[0] if exc.args else "ingestion_failed",
            )
            raise

        async with session_factory() as session:
            version = await session.scalar(
                select(KnowledgeDocumentVersion)
                .where(
                    KnowledgeDocumentVersion.id == version_id,
                    KnowledgeDocumentVersion.document_id == document_id,
                    KnowledgeDocumentVersion.organization_id == organization_id,
                )
                .with_for_update()
            )
            document = await session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.organization_id == organization_id,
                )
                .with_for_update()
            )
            if version is None or document is None or document.status != "active":
                await provider.delete_file(provider_store_id, provider_file_id)
                if version is not None:
                    version.status = "deleted"
                    version.error_code = None
                    await session.commit()
                return
            latest_ready_version = await session.scalar(
                select(func.max(KnowledgeDocumentVersion.version_number)).where(
                    KnowledgeDocumentVersion.organization_id == organization_id,
                    KnowledgeDocumentVersion.document_id == document_id,
                    KnowledgeDocumentVersion.status == "ready",
                )
            )
            if latest_ready_version is not None and latest_ready_version > version.version_number:
                version.provider_file_id = provider_file_id
                version.status = "superseded"
                version.error_code = None
                await session.commit()
                await self._delete_superseded_files(
                    session_factory,
                    organization_id,
                    document_id,
                )
                return

            old_versions = list(
                (
                    await session.scalars(
                        select(KnowledgeDocumentVersion)
                        .where(
                            KnowledgeDocumentVersion.organization_id == organization_id,
                            KnowledgeDocumentVersion.document_id == document_id,
                            KnowledgeDocumentVersion.id != version_id,
                            KnowledgeDocumentVersion.status == "ready",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for previous in old_versions:
                previous.status = "superseded"
            version.provider_file_id = provider_file_id
            version.status = "ready"
            version.error_code = None
            version.ingested_at = datetime.now(UTC)
            await session.commit()
        await self._delete_superseded_files(
            session_factory,
            organization_id,
            document_id,
        )

    async def _delete_superseded_files(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        organization_id: UUID,
        document_id: UUID,
    ) -> None:
        provider = self.require_provider()
        async with session_factory() as session:
            store = await session.scalar(
                select(KnowledgeStore).where(
                    KnowledgeStore.organization_id == organization_id,
                )
            )
            if store is None:
                return
            if store.provider != provider.name:
                raise KnowledgeProviderError("provider_configuration_changed")
            if store.status == "failed":
                raise KnowledgeProviderError(store.error_code or "provider_store_failed")
            if store.status != "ready" or store.provider_store_id is None:
                return
            provider_store_id = store.provider_store_id
            stale_versions = list(
                (
                    await session.scalars(
                        select(KnowledgeDocumentVersion).where(
                            KnowledgeDocumentVersion.organization_id == organization_id,
                            KnowledgeDocumentVersion.document_id == document_id,
                            KnowledgeDocumentVersion.status == "superseded",
                            KnowledgeDocumentVersion.provider_file_id.is_not(None),
                        )
                    )
                ).all()
            )
            stale_files = [
                (version.id, version.provider_file_id)
                for version in stale_versions
                if version.provider_file_id is not None
            ]
        for version_id, file_id in stale_files:
            await provider.delete_file(provider_store_id, file_id)
            async with session_factory() as session:
                version = await session.scalar(
                    select(KnowledgeDocumentVersion)
                    .where(
                        KnowledgeDocumentVersion.id == version_id,
                        KnowledgeDocumentVersion.organization_id == organization_id,
                        KnowledgeDocumentVersion.status == "superseded",
                        KnowledgeDocumentVersion.provider_file_id == file_id,
                    )
                    .with_for_update()
                )
                if version is not None:
                    version.provider_file_id = None
                    await session.commit()

    async def _mark_version_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        organization_id: UUID,
        version_id: UUID,
        error_code: str,
    ) -> None:
        async with session_factory() as session:
            version = await session.scalar(
                select(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.id == version_id,
                    KnowledgeDocumentVersion.organization_id == organization_id,
                )
            )
            if version is not None and version.status != "deleted":
                version.status = "failed"
                version.error_code = error_code
                await session.commit()

    async def retry_ingestion(
        self,
        session: AsyncSession,
        organization_id: UUID,
        document_id: UUID,
        version_id: UUID,
    ) -> KnowledgeDocumentVersion:
        self.require_provider()
        version = await session.scalar(
            select(KnowledgeDocumentVersion)
            .where(
                KnowledgeDocumentVersion.id == version_id,
                KnowledgeDocumentVersion.document_id == document_id,
                KnowledgeDocumentVersion.organization_id == organization_id,
                KnowledgeDocumentVersion.status == "failed",
            )
            .with_for_update()
        )
        document = await session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.organization_id == organization_id,
                KnowledgeDocument.status == "active",
            )
        )
        if version is None or document is None:
            raise KnowledgeDocumentNotFound
        version.status = "queued"
        version.error_code = None
        await session.commit()
        await session.refresh(version)
        return version

    async def enqueue_deletion(
        self,
        session: AsyncSession,
        organization_id: UUID,
        document_id: UUID,
    ) -> KnowledgeDocument:
        self.require_provider()
        document = await session.scalar(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.organization_id == organization_id,
                KnowledgeDocument.status != "deleted",
            )
            .with_for_update()
        )
        if document is None:
            raise KnowledgeDocumentNotFound
        document.status = "deleting"
        await session.commit()
        try:
            celery_app.send_task(
                KNOWLEDGE_DELETE_TASK_NAME,
                args=[str(organization_id), str(document_id)],
            )
        except Exception as exc:
            logger.error(
                "Unable to enqueue knowledge deletion",
                extra={"document_id": str(document_id), "error_type": type(exc).__name__},
            )
            raise KnowledgeJobDispatchError from exc
        return document

    async def delete_document(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        organization_id: UUID,
        document_id: UUID,
    ) -> None:
        provider = self.require_provider()
        async with session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.organization_id == organization_id,
                )
                .with_for_update()
            )
            if document is None or document.status == "deleted":
                return
            store = await session.scalar(
                select(KnowledgeStore).where(KnowledgeStore.organization_id == organization_id)
            )
            if store is not None and store.provider != provider.name:
                raise KnowledgeProviderError("provider_configuration_changed")
            versions = list(
                (
                    await session.scalars(
                        select(KnowledgeDocumentVersion)
                        .where(
                            KnowledgeDocumentVersion.organization_id == organization_id,
                            KnowledgeDocumentVersion.document_id == document_id,
                        )
                        .with_for_update()
                    )
                ).all()
            )
            store_id = store.provider_store_id if store is not None else None
            sources = [(version.object_key, version.provider_file_id) for version in versions]

        if store_id is not None:
            for _, file_id in sources:
                if file_id is not None:
                    await provider.delete_file(store_id, file_id)
        for object_key, _ in sources:
            await self.storage.delete(object_key)

        async with session_factory() as session:
            document = await session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.organization_id == organization_id,
                )
                .with_for_update()
            )
            versions = await session.scalars(
                select(KnowledgeDocumentVersion)
                .where(
                    KnowledgeDocumentVersion.organization_id == organization_id,
                    KnowledgeDocumentVersion.document_id == document_id,
                )
                .with_for_update()
            )
            if document is not None:
                document.status = "deleted"
            for version in versions:
                version.status = "deleted"
                version.provider_file_id = None
                version.error_code = None
            await session.commit()


def create_knowledge_service() -> KnowledgeService:
    return KnowledgeService(
        provider=create_knowledge_provider(),
        storage=S3KnowledgeObjectStorage(),
    )
