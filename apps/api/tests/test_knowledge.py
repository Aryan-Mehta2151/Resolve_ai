import asyncio
from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.core.celery import KNOWLEDGE_DELETE_TASK_NAME, KNOWLEDGE_INGEST_TASK_NAME, celery_app
from app.modules.agent.graph import AgentState, build_agent_graph
from app.modules.knowledge.provider import KnowledgeProviderError, ProviderAnswer, ProviderCitation
from app.modules.knowledge.router import get_knowledge_service
from app.modules.knowledge.schemas import KnowledgeAnswer, KnowledgeCitation
from app.modules.knowledge.service import NO_KNOWLEDGE_ANSWER, KnowledgeService


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    async def put(self, object_key: str, content: bytes, content_type: str) -> None:
        self.objects[object_key] = content
        self.content_types[object_key] = content_type

    async def get(self, object_key: str) -> bytes:
        return self.objects[object_key]

    async def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)
        self.content_types.pop(object_key, None)

    async def check_health(self) -> None:
        return None


class MemoryProvider:
    name = "openai_file_search"

    def __init__(self) -> None:
        self.stores: dict[str, dict[str, tuple[str, bytes]]] = {}
        self.answers: list[str] = []
        self.answer_overrides: dict[str, ProviderAnswer] = {}
        self._next_file_number = 1
        self.fail_next_ingestion = False

    async def create_store(self, organization_id: UUID) -> str:
        store_id = f"store-{organization_id}"
        self.stores.setdefault(store_id, {})
        return store_id

    async def ingest_file(self, store_id: str, filename: str, content: bytes) -> str:
        if self.fail_next_ingestion:
            self.fail_next_ingestion = False
            raise KnowledgeProviderError("provider_ingestion_failed")
        file_id = f"file-{self._next_file_number}"
        self._next_file_number += 1
        self.stores[store_id][file_id] = (filename, content)
        return file_id

    async def delete_file(self, store_id: str, file_id: str) -> None:
        self.stores.get(store_id, {}).pop(file_id, None)

    async def answer(self, store_id: str, question: str) -> ProviderAnswer:
        self.answers.append(store_id)
        if store_id in self.answer_overrides:
            return self.answer_overrides[store_id]
        files = self.stores.get(store_id, {})
        if not files or "unknown" in question.casefold():
            return ProviderAnswer("Unsupported answer from model.", ())
        file_id, (filename, content) = next(iter(files.items()))
        return ProviderAnswer(
            "The return period is 30 days.",
            (
                ProviderCitation(
                    file_id=file_id,
                    filename=filename,
                    excerpt=content.decode("utf-8")[:100],
                ),
            ),
        )


def register(client: TestClient, email: str, slug: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Knowledge Test",
            "organization_slug": slug,
            "full_name": "Knowledge Admin",
            "email": email,
            "password": "Correct-Horse-Battery-1!",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def knowledge_runtime(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[KnowledgeService, list[tuple[str, list[str]]]], None, None]:
    provider = MemoryProvider()
    storage = MemoryStorage()
    service = KnowledgeService(provider, storage)
    jobs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda name, args: jobs.append((name, args)),
    )
    client.app.dependency_overrides[get_knowledge_service] = lambda: service
    yield service, jobs


def upload(
    client: TestClient,
    token: str,
    *,
    title: str = "Returns policy",
    content: bytes = b"Our standard return period is 30 days.",
) -> dict:
    response = client.post(
        "/api/v1/knowledge/documents",
        headers=auth_headers(token),
        data={"title": title},
        files={"file": ("returns.md", content, "text/markdown")},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_knowledge_upload_ingestion_search_and_tenant_isolation(
    client: TestClient,
    database_session_factory,
    knowledge_runtime: tuple[KnowledgeService, list[tuple[str, list[str]]]],
) -> None:
    service, jobs = knowledge_runtime
    provider = service.provider
    assert isinstance(provider, MemoryProvider)
    first_tenant = register(client, "knowledge-one@example.com", "knowledge-one")
    second_tenant = register(client, "knowledge-two@example.com", "knowledge-two")

    uploaded = upload(client, first_tenant["access_token"])
    version = uploaded["version"]
    assert version["status"] == "queued"
    assert jobs == [
        (
            KNOWLEDGE_INGEST_TASK_NAME,
            [
                first_tenant["organization"]["id"],
                uploaded["document"]["id"],
                version["id"],
            ],
        )
    ]

    async def ingest() -> None:
        await service.ingest_version(
            database_session_factory,
            UUID(first_tenant["organization"]["id"]),
            UUID(uploaded["document"]["id"]),
            UUID(version["id"]),
        )

    asyncio.run(ingest())
    details = client.get(
        f"/api/v1/knowledge/documents/{uploaded['document']['id']}",
        headers=auth_headers(first_tenant["access_token"]),
    )
    assert details.status_code == 200
    assert details.json()["versions"][0]["status"] == "ready"

    found = client.post(
        "/api/v1/knowledge/search",
        headers=auth_headers(first_tenant["access_token"]),
        json={"query": "How long do I have to return an item?"},
    )
    assert found.status_code == 200, found.text
    assert found.json()["grounded"] is True
    assert found.json()["citations"][0]["document_id"] == uploaded["document"]["id"]
    assert found.json()["citations"][0]["title"] == "Returns policy"

    missing = client.post(
        "/api/v1/knowledge/search",
        headers=auth_headers(second_tenant["access_token"]),
        json={"query": "How long do I have to return an item?"},
    )
    assert missing.status_code == 200
    assert missing.json() == {
        "answer": NO_KNOWLEDGE_ANSWER,
        "citations": [],
        "grounded": False,
    }
    forbidden_document = client.get(
        f"/api/v1/knowledge/documents/{uploaded['document']['id']}",
        headers=auth_headers(second_tenant["access_token"]),
    )
    assert forbidden_document.status_code == 404
    assert provider.answers and provider.answers == [f"store-{first_tenant['organization']['id']}"]

    second_upload = upload(
        client,
        second_tenant["access_token"],
        title="Second tenant policy",
        content=b"This policy belongs only to another organization.",
    )

    async def ingest_second_tenant() -> None:
        await service.ingest_version(
            database_session_factory,
            UUID(second_tenant["organization"]["id"]),
            UUID(second_upload["document"]["id"]),
            UUID(second_upload["version"]["id"]),
        )

    asyncio.run(ingest_second_tenant())
    first_file_id = next(iter(provider.stores[f"store-{first_tenant['organization']['id']}"]))
    provider.answer_overrides[f"store-{second_tenant['organization']['id']}"] = ProviderAnswer(
        "A cross-tenant answer must be discarded.",
        (ProviderCitation(first_file_id, "returns.md"),),
    )
    cross_tenant = client.post(
        "/api/v1/knowledge/search",
        headers=auth_headers(second_tenant["access_token"]),
        json={"query": "Tell me about your return policy."},
    )
    assert cross_tenant.json()["grounded"] is False
    assert cross_tenant.json()["answer"] == NO_KNOWLEDGE_ANSWER


def test_document_versions_supersede_and_delete_provider_assets(
    client: TestClient,
    database_session_factory,
    knowledge_runtime: tuple[KnowledgeService, list[tuple[str, list[str]]]],
) -> None:
    service, jobs = knowledge_runtime
    provider = service.provider
    storage = service.storage
    assert isinstance(provider, MemoryProvider)
    organization = register(client, "knowledge-version@example.com", "knowledge-version")
    headers = auth_headers(organization["access_token"])
    uploaded = upload(client, organization["access_token"])

    async def ingest_version(version_id: str) -> None:
        await service.ingest_version(
            database_session_factory,
            UUID(organization["organization"]["id"]),
            UUID(uploaded["document"]["id"]),
            UUID(version_id),
        )

    asyncio.run(ingest_version(uploaded["version"]["id"]))
    old_files = set(provider.stores[f"store-{organization['organization']['id']}"])
    replacement = client.post(
        f"/api/v1/knowledge/documents/{uploaded['document']['id']}/versions",
        headers=headers,
        files={"file": ("returns.md", b"Returns are accepted within 45 days.", "text/markdown")},
    )
    assert replacement.status_code == 202, replacement.text
    asyncio.run(ingest_version(replacement.json()["version"]["id"]))
    assert set(provider.stores[f"store-{organization['organization']['id']}"]) != old_files

    details = client.get(
        f"/api/v1/knowledge/documents/{uploaded['document']['id']}",
        headers=headers,
    ).json()
    assert [item["status"] for item in details["versions"]] == ["ready", "superseded"]
    answer = client.post(
        "/api/v1/knowledge/search",
        headers=headers,
        json={"query": "What is the return period?"},
    ).json()
    assert answer["citations"][0]["version_id"] == replacement.json()["version"]["id"]

    deleted = client.delete(
        f"/api/v1/knowledge/documents/{uploaded['document']['id']}",
        headers=headers,
    )
    assert deleted.status_code == 202
    assert jobs[-1] == (
        KNOWLEDGE_DELETE_TASK_NAME,
        [organization["organization"]["id"], uploaded["document"]["id"]],
    )

    async def delete() -> None:
        await service.delete_document(
            database_session_factory,
            UUID(organization["organization"]["id"]),
            UUID(uploaded["document"]["id"]),
        )

    asyncio.run(delete())
    assert provider.stores[f"store-{organization['organization']['id']}"] == {}
    assert storage.objects == {}
    assert (
        client.get(
            f"/api/v1/knowledge/documents/{uploaded['document']['id']}",
            headers=headers,
        ).status_code
        == 404
    )


def test_upload_validation_rbac_and_no_citation_safe_answer(
    client: TestClient,
    database_session_factory,
    knowledge_runtime: tuple[KnowledgeService, list[tuple[str, list[str]]]],
) -> None:
    service, _ = knowledge_runtime
    organization = register(client, "knowledge-roles@example.com", "knowledge-roles")
    admin_headers = auth_headers(organization["access_token"])
    invalid = client.post(
        "/api/v1/knowledge/documents",
        headers=admin_headers,
        data={"title": "Invalid upload"},
        files={"file": ("malware.exe", b"not a document", "application/octet-stream")},
    )
    assert invalid.status_code == 422

    membership = client.post(
        f"/api/v1/organizations/{organization['organization']['id']}/memberships",
        headers=admin_headers,
        json={
            "full_name": "Read Only User",
            "email": "knowledge-viewer@example.com",
            "password": "Correct-Horse-Battery-2!",
            "role": "viewer",
        },
    )
    assert membership.status_code == 201, membership.text
    viewer_login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "knowledge-viewer@example.com",
            "password": "Correct-Horse-Battery-2!",
        },
    )
    assert viewer_login.status_code == 200
    viewer_headers = auth_headers(viewer_login.json()["access_token"])
    denied = client.post(
        "/api/v1/knowledge/documents",
        headers=viewer_headers,
        data={"title": "Forbidden"},
        files={"file": ("returns.md", b"Text", "text/markdown")},
    )
    assert denied.status_code == 403

    uploaded = upload(client, organization["access_token"])

    async def ingest() -> None:
        await service.ingest_version(
            database_session_factory,
            UUID(organization["organization"]["id"]),
            UUID(uploaded["document"]["id"]),
            UUID(uploaded["version"]["id"]),
        )

    asyncio.run(ingest())
    no_citation = client.post(
        "/api/v1/knowledge/search",
        headers=admin_headers,
        json={"query": "unknown detail"},
    )
    assert no_citation.status_code == 200
    assert no_citation.json()["answer"] == NO_KNOWLEDGE_ANSWER
    assert no_citation.json()["grounded"] is False


def test_failed_ingestion_can_be_retried(
    client: TestClient,
    database_session_factory,
    knowledge_runtime: tuple[KnowledgeService, list[tuple[str, list[str]]]],
) -> None:
    service, jobs = knowledge_runtime
    provider = service.provider
    assert isinstance(provider, MemoryProvider)
    organization = register(client, "knowledge-retry@example.com", "knowledge-retry")
    uploaded = upload(client, organization["access_token"])
    version_id = uploaded["version"]["id"]
    document_id = uploaded["document"]["id"]
    organization_id = UUID(organization["organization"]["id"])
    provider.fail_next_ingestion = True

    async def ingest() -> None:
        await service.ingest_version(
            database_session_factory,
            organization_id,
            UUID(document_id),
            UUID(version_id),
        )

    with pytest.raises(KnowledgeProviderError):
        asyncio.run(ingest())
    failed = client.get(
        f"/api/v1/knowledge/documents/{document_id}",
        headers=auth_headers(organization["access_token"]),
    ).json()["versions"][0]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "provider_ingestion_failed"

    retry = client.post(
        f"/api/v1/knowledge/documents/{document_id}/versions/{version_id}/retry",
        headers=auth_headers(organization["access_token"]),
    )
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"
    assert jobs[-1][0] == KNOWLEDGE_INGEST_TASK_NAME
    asyncio.run(ingest())
    ready = client.get(
        f"/api/v1/knowledge/documents/{document_id}",
        headers=auth_headers(organization["access_token"]),
    ).json()["versions"][0]
    assert ready["status"] == "ready"


def test_agent_includes_sources_for_grounded_knowledge_answer() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    citation = KnowledgeCitation(
        document_id=UUID("00000000-0000-0000-0000-000000000001"),
        version_id=UUID("00000000-0000-0000-0000-000000000002"),
        title="Returns policy",
        filename="returns.md",
    )

    async def record_event(name: str, payload: dict[str, object]) -> None:
        events.append((name, payload))

    async def answer_question(question: str) -> KnowledgeAnswer:
        assert "return" in question.casefold()
        return KnowledgeAnswer(answer="You have 30 days to return an item.", citations=[citation])

    state: AgentState = {
        "conversation_id": "conversation-1",
        "organization_id": "organization-1",
        "workflow_id": "workflow-1",
        "messages": [{"role": "user", "content": "What is the return policy?"}],
        "latest_message": "What is the return policy?",
    }
    graph = build_agent_graph(
        InMemorySaver(),
        record_event,
        knowledge_answerer=answer_question,
    )
    result = asyncio.run(graph.ainvoke(state, config={"configurable": {"thread_id": "c1"}}))
    assert result["intent"] == "general"
    assert "You have 30 days" in result["response"]
    assert "Sources: [1] Returns policy (returns.md)" in result["response"]
    assert any(event == "knowledge.retrieved" for event, _ in events)


def test_provider_response_parser_extracts_managed_file_citations() -> None:
    from app.modules.knowledge.provider import _extract_answer_and_citations

    result = _extract_answer_and_citations(
        {
            "output": [
                {
                    "type": "file_search_call",
                    "results": [
                        {
                            "file_id": "file-1",
                            "filename": "returns.md",
                            "text": "The standard return period is 30 days.",
                        }
                    ],
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "The return period is 30 days.",
                            "annotations": [
                                {
                                    "type": "file_citation",
                                    "file_id": "file-1",
                                    "filename": "returns.md",
                                }
                            ],
                        }
                    ],
                },
            ]
        }
    )
    assert result.answer == "The return period is 30 days."
    assert result.citations[0].file_id == "file-1"
    assert result.citations[0].excerpt == "The standard return period is 30 days."
