import logging
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, CurrentUser, SessionDep, require_roles
from app.core.config import settings
from app.db.models import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    MembershipRole,
)
from app.modules.knowledge.schemas import (
    KnowledgeDocumentResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeUploadResponse,
    KnowledgeVersionResponse,
)
from app.modules.knowledge.service import (
    NO_KNOWLEDGE_ANSWER,
    KnowledgeDocumentNotFound,
    KnowledgeJobDispatchError,
    KnowledgeProviderError,
    KnowledgeProviderNotConfigured,
    KnowledgeService,
)
from app.modules.knowledge.storage import KnowledgeStorageError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
KnowledgeEditor = Annotated[
    AuthenticatedUser,
    Depends(require_roles(MembershipRole.ADMIN, MembershipRole.SUPERVISOR)),
]


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge_service


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


async def _document_response(
    document: KnowledgeDocument,
    session: AsyncSession,
    version_limit: int = 20,
) -> KnowledgeDocumentResponse:
    versions = await session.scalars(
        select(KnowledgeDocumentVersion)
        .where(
            KnowledgeDocumentVersion.organization_id == document.organization_id,
            KnowledgeDocumentVersion.document_id == document.id,
        )
        .order_by(KnowledgeDocumentVersion.version_number.desc())
        .limit(version_limit)
    )
    return KnowledgeDocumentResponse(
        id=document.id,
        title=document.title,
        description=document.description,
        status=document.status,
        created_at=document.created_at,
        updated_at=document.updated_at,
        versions=[KnowledgeVersionResponse.model_validate(version) for version in versions],
    )


def _require_provider(service: KnowledgeService) -> None:
    try:
        service.require_provider()
    except KnowledgeProviderNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed knowledge retrieval is not configured",
        ) from exc


@router.post(
    "/documents",
    response_model=KnowledgeUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    user: KnowledgeEditor,
    session: SessionDep,
    service: KnowledgeServiceDep,
    title: Annotated[str, Form(min_length=1, max_length=160)],
    file: Annotated[UploadFile, File()],
    description: Annotated[str | None, Form(max_length=500)] = None,
) -> KnowledgeUploadResponse:
    _require_provider(service)
    normalized_title = title.strip()
    if not normalized_title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The document title must not be blank",
        )
    content = await file.read(settings.knowledge_max_file_bytes + 1)
    try:
        filename, content_type = service.validate_upload(file.filename, file.content_type, content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    try:
        document, version = await service.create_upload(
            session,
            user,
            normalized_title,
            description.strip() if description else None,
            filename,
            content_type,
            content,
        )
    except KnowledgeProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The knowledge provider configuration is unavailable",
        ) from exc
    except KnowledgeDocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        ) from exc
    except KnowledgeStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is unavailable",
        ) from exc

    try:
        await service.enqueue_ingestion(
            session,
            user.organization_id,
            document.id,
            version.id,
        )
    except KnowledgeJobDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Document upload was saved but ingestion could not be queued",
                "version_id": str(version.id),
            },
        ) from exc
    response_document = await _document_response(document, session)
    response_version = await session.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.id == version.id,
            KnowledgeDocumentVersion.organization_id == user.organization_id,
        )
    )
    if response_version is None:
        raise RuntimeError("New knowledge version was not persisted")
    return KnowledgeUploadResponse(
        document=response_document,
        version=KnowledgeVersionResponse.model_validate(response_version),
    )


@router.post(
    "/documents/{document_id}/versions",
    response_model=KnowledgeUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document_version(
    document_id: UUID,
    user: KnowledgeEditor,
    session: SessionDep,
    service: KnowledgeServiceDep,
    file: Annotated[UploadFile, File()],
) -> KnowledgeUploadResponse:
    _require_provider(service)
    content = await file.read(settings.knowledge_max_file_bytes + 1)
    try:
        filename, content_type = service.validate_upload(file.filename, file.content_type, content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    document = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == user.organization_id,
            KnowledgeDocument.status == "active",
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    try:
        document, version = await service.create_upload(
            session,
            user,
            document.title,
            document.description,
            filename,
            content_type,
            content,
            document_id,
        )
    except KnowledgeProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The knowledge provider configuration is unavailable",
        ) from exc
    except KnowledgeDocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        ) from exc
    except KnowledgeStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is unavailable",
        ) from exc
    try:
        await service.enqueue_ingestion(
            session,
            user.organization_id,
            document.id,
            version.id,
        )
    except KnowledgeJobDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Document version was saved but ingestion could not be queued",
                "version_id": str(version.id),
            },
        ) from exc
    return KnowledgeUploadResponse(
        document=await _document_response(document, session),
        version=KnowledgeVersionResponse.model_validate(version),
    )


@router.get("/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    user: CurrentUser,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> list[KnowledgeDocumentResponse]:
    documents = list(
        (
            await session.scalars(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.organization_id == user.organization_id,
                    KnowledgeDocument.status != "deleted",
                )
                .order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    if not documents:
        return []
    document_ids = {document.id for document in documents}
    latest_versions = (
        select(
            KnowledgeDocumentVersion.organization_id,
            KnowledgeDocumentVersion.document_id,
            func.max(KnowledgeDocumentVersion.version_number).label("latest_version"),
        )
        .where(
            KnowledgeDocumentVersion.organization_id == user.organization_id,
            KnowledgeDocumentVersion.document_id.in_(document_ids),
        )
        .group_by(
            KnowledgeDocumentVersion.organization_id,
            KnowledgeDocumentVersion.document_id,
        )
        .subquery()
    )
    versions = await session.scalars(
        select(KnowledgeDocumentVersion).join(
            latest_versions,
            (KnowledgeDocumentVersion.organization_id == latest_versions.c.organization_id)
            & (KnowledgeDocumentVersion.document_id == latest_versions.c.document_id)
            & (KnowledgeDocumentVersion.version_number == latest_versions.c.latest_version),
        )
    )
    latest_by_document = {version.document_id: version for version in versions}
    return [
        KnowledgeDocumentResponse(
            id=document.id,
            title=document.title,
            description=document.description,
            status=document.status,
            created_at=document.created_at,
            updated_at=document.updated_at,
            versions=(
                [KnowledgeVersionResponse.model_validate(latest_by_document[document.id])]
                if document.id in latest_by_document
                else []
            ),
        )
        for document in documents
    ]


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentResponse)
async def get_document(
    document_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    version_limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KnowledgeDocumentResponse:
    document = await session.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.organization_id == user.organization_id,
            KnowledgeDocument.status != "deleted",
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return await _document_response(document, session, version_limit)


@router.post(
    "/documents/{document_id}/versions/{version_id}/retry",
    response_model=KnowledgeVersionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_document_ingestion(
    document_id: UUID,
    version_id: UUID,
    user: KnowledgeEditor,
    session: SessionDep,
    service: KnowledgeServiceDep,
) -> KnowledgeVersionResponse:
    try:
        version = await service.retry_ingestion(
            session,
            user.organization_id,
            document_id,
            version_id,
        )
    except KnowledgeProviderNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed knowledge retrieval is not configured",
        ) from exc
    except KnowledgeDocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failed document version not found",
        ) from exc
    try:
        await service.enqueue_ingestion(
            session,
            user.organization_id,
            document_id,
            version_id,
        )
    except KnowledgeJobDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Ingestion retry could not be queued",
                "version_id": str(version_id),
            },
        ) from exc
    return KnowledgeVersionResponse.model_validate(version)


@router.delete("/documents/{document_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_document(
    document_id: UUID,
    user: KnowledgeEditor,
    session: SessionDep,
    service: KnowledgeServiceDep,
) -> dict[str, str]:
    try:
        document = await service.enqueue_deletion(session, user.organization_id, document_id)
    except KnowledgeProviderNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed knowledge retrieval is not configured",
        ) from exc
    except KnowledgeDocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        ) from exc
    except KnowledgeJobDispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Document deletion was accepted but could not be queued",
                "document_id": str(document_id),
            },
        ) from exc
    return {"id": str(document.id), "status": document.status}


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    user: CurrentUser,
    session: SessionDep,
    service: KnowledgeServiceDep,
) -> KnowledgeSearchResponse:
    try:
        answer = await service.answer(session, user.organization_id, payload.query.strip())
    except KnowledgeProviderNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed knowledge retrieval is not configured",
        ) from exc
    except KnowledgeProviderError as exc:
        logger.error(
            "Managed knowledge search failed",
            extra={"organization_id": str(user.organization_id), "error_code": exc.error_code},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Knowledge search is temporarily unavailable",
        ) from exc
    if answer is None:
        return KnowledgeSearchResponse(answer=NO_KNOWLEDGE_ANSWER, citations=[], grounded=False)
    return KnowledgeSearchResponse(
        answer=answer.answer,
        citations=answer.citations,
        grounded=bool(answer.citations),
    )
