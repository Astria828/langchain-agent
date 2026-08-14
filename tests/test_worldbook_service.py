"""阶段 4 世界书与正式条目服务单元测试。"""

import asyncio
import json
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import protect_api_key
from app.db.database import create_database_engine, create_session_factory
from app.db.models import BackgroundTask, Character, ChatSession, ModelConfig
from app.db.models import WorldBookEntry as WorldBookEntryModel
from app.gateways.model_gateway import ModelGateway
from app.repositories.chroma_repository import (
    WORLD_BOOK_COLLECTION,
    ChromaRepository,
    build_worldbook_index_text,
    calculate_content_hash,
    worldbook_entry_document_id,
)
from app.schemas.dto import (
    ConfirmWorldBookDraftsPayload,
    UpdateWorldBookEntryPayload,
    UpdateWorldBookPayload,
)
from app.services.worldbook_service import WorldBookService
from sqlalchemy import func, select

from scripts.init_db import initialize_database


class StubGateway:
    """返回固定拆分结果或向量的模型网关。"""

    def __init__(
        self,
        *,
        embeddings: list[list[float]] | None = None,
        embedding_error: AppError | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.embedding_error = embedding_error
        self.embedding_request: dict[str, object] | None = None
        self.on_embed = None

    async def create_chat_completion(self, **_kwargs) -> str:
        """避免服务测试访问真实模型。"""

        return '{"drafts":[{"name":"银港","category":"地点","content":"银港终年被海雾笼罩。"}]}'

    async def create_embeddings(self, **kwargs) -> list[list[float]]:
        """记录向量请求，并按测试场景返回或抛出错误。"""

        self.embedding_request = kwargs
        if self.on_embed is not None:
            self.on_embed()
        if self.embedding_error is not None:
            raise self.embedding_error
        if self.embeddings is not None:
            return self.embeddings
        return [[0.1, 0.2, 0.3] for _text in kwargs["texts"]]


class RecordingChroma:
    """记录世界书向量写入，并可模拟存储失败。"""

    def __init__(self, *, fail: bool = False, delete_fail: bool = False) -> None:
        self.fail = fail
        self.delete_fail = delete_fail
        self.calls: list[dict[str, object]] = []
        self.deleted_ids: list[str] = []

    def upsert_worldbook(self, **kwargs) -> None:
        """保存调用参数，或抛出受控的 Chroma 写入错误。"""

        if self.fail:
            raise RuntimeError("chroma unavailable")
        self.calls.append(kwargs)

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """记录确定 ID 清理，或模拟 Chroma 删除失败。"""

        if self.delete_fail:
            raise RuntimeError("chroma unavailable")
        self.deleted_ids.extend(ids)


def create_service(tmp_path: Path, gateway=None, chroma_repository=None):
    """创建使用隔离数据库的世界书服务与 Session。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session = create_session_factory(engine)()
    return (
        WorldBookService(
            session,
            gateway or ModelGateway(),
            chroma_repository or RecordingChroma(),
        ),
        session,
        engine,
    )


def configure_embedding(session, *, version: int = 3) -> None:
    """为确认入库测试配置可用的 Embedding 端点。"""

    config = session.get(ModelConfig, "embed")
    assert config is not None
    config.base_url = "https://models.example/v1"
    config.model_name = "embed-model"
    config.secret_ref = protect_api_key("sk-test-embed")
    config.vector_dimension = 3
    config.config_version = version
    session.commit()


def test_world_book_and_entry_crud_preserves_order_and_index_state(
    tmp_path: Path,
) -> None:
    """世界书与条目可持久化，条目按位置排序且索引文本变化后过期。"""

    service, session, engine = create_service(tmp_path)
    try:
        book = service.create_world_book()
        assert book.name == "新世界书"
        assert book.raw_content == ""
        assert book.entries == []

        saved = service.update_world_book(
            book.id,
            UpdateWorldBookPayload(name="  北境设定集  ", raw_content="完整原文"),
        )
        assert saved.name == "北境设定集"
        assert saved.raw_content == "完整原文"

        first = service.create_entry(book.id)
        second = service.create_entry(book.id)
        assert [entry.id for entry in service.get_world_book(book.id).entries] == [
            first.id,
            second.id,
        ]

        model = session.get(WorldBookEntryModel, first.id)
        embed_config = session.get(ModelConfig, "embed")
        assert model is not None and embed_config is not None
        embed_config.config_version = 2
        model.index_status = "ready"
        model.embedding_version = 2
        old_hash = model.content_hash
        session.commit()

        updated = service.update_entry(
            book.id,
            first.id,
            UpdateWorldBookEntryPayload(
                category="地点",
                keywords=[" 北境 ", "北境", "雾海"],
                resident=True,
            ),
        )
        assert updated.category == "地点"
        assert updated.keywords == ["北境", "雾海"]
        assert updated.resident is True
        assert updated.index_stale is True
        assert model.index_status == "stale"
        assert model.content_hash != old_hash

        service.delete_entry(book.id, second.id)
        assert [entry.id for entry in service.get_world_book(book.id).entries] == [
            first.id
        ]

        session.close()
        with create_session_factory(engine)() as reopened_session:
            persisted = WorldBookService(
                reopened_session,
                ModelGateway(),
                RecordingChroma(),
            ).get_world_book(book.id)
            assert persisted.name == "北境设定集"
            assert [entry.id for entry in persisted.entries] == [first.id]
    finally:
        session.close()
        engine.dispose()


def test_world_book_delete_rejects_historical_session_reference(tmp_path: Path) -> None:
    """历史会话引用存在时返回 409，且世界书仍然保留。"""

    service, session, engine = create_service(tmp_path)
    try:
        book = service.create_world_book()
        character = Character(
            name="归舟",
            introduction="简介",
            system_prompt="提示词",
            dialogue_examples_json="[]",
        )
        session.add(character)
        session.flush()
        session.add(
            ChatSession(
                title="历史会话",
                character_id=character.id,
                world_book_id=book.id,
            )
        )
        session.commit()

        with pytest.raises(AppError) as error:
            service.delete_world_book(book.id)

        assert error.value.status_code == 409
        assert error.value.code == "RESOURCE_IN_USE"
        assert service.get_world_book(book.id).id == book.id
    finally:
        session.close()
        engine.dispose()


def test_entry_lookup_is_scoped_to_its_world_book(tmp_path: Path) -> None:
    """条目 ID 不能用于修改另一本世界书的数据。"""

    service, session, engine = create_service(tmp_path)
    try:
        first_book = service.create_world_book()
        second_book = service.create_world_book()
        entry = service.create_entry(first_book.id)

        with pytest.raises(AppError) as error:
            service.update_entry(
                second_book.id,
                entry.id,
                UpdateWorldBookEntryPayload(name="越界修改"),
            )

        assert error.value.status_code == 404
        assert service.get_world_book(first_book.id).entries[0].name == "新条目"
    finally:
        session.close()
        engine.dispose()


def test_split_returns_transient_drafts_without_creating_entries(
    tmp_path: Path,
) -> None:
    """拆分结果只返回给调用方，确认前不写入正式条目。"""

    service, session, engine = create_service(tmp_path, StubGateway())
    try:
        book = service.create_world_book()
        service.update_world_book(
            book.id, UpdateWorldBookPayload(raw_content="银港终年被海雾笼罩。")
        )
        config = session.get(ModelConfig, "main")
        assert config is not None
        config.base_url = "https://models.example/v1"
        config.model_name = "chat-model"
        config.secret_ref = protect_api_key("sk-test-main")
        session.commit()

        drafts = asyncio.run(service.split_world_book(book.id))

        assert drafts[0].name == "银港"
        assert service.get_world_book(book.id).entries == []
    finally:
        session.close()
        engine.dispose()


def test_split_rejects_empty_source_and_missing_model_configuration(
    tmp_path: Path,
) -> None:
    """空原文和未配置主模型分别返回稳定业务错误。"""

    service, session, engine = create_service(tmp_path, StubGateway())
    try:
        book = service.create_world_book()
        with pytest.raises(AppError) as empty_error:
            asyncio.run(service.split_world_book(book.id))
        assert empty_error.value.code == "EMPTY_SOURCE_CONTENT"

        service.update_world_book(
            book.id, UpdateWorldBookPayload(raw_content="有效原文")
        )
        with pytest.raises(AppError) as config_error:
            asyncio.run(service.split_world_book(book.id))
        assert config_error.value.status_code == 503
        assert config_error.value.code == "MODEL_NOT_CONFIGURED"
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_commits_sqlite_before_embedding_and_marks_ready(
    tmp_path: Path,
) -> None:
    """正式条目先提交，再按稳定文本与 metadata 写入当前版本向量。"""

    gateway = StubGateway(embeddings=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, gateway, chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session, version=3)
        session_factory = create_session_factory(engine)

        def assert_entries_committed() -> None:
            with session_factory() as other_session:
                count = other_session.scalar(
                    select(func.count(WorldBookEntryModel.id)).where(
                        WorldBookEntryModel.world_book_id == book.id
                    )
                )
                assert count == 2

        gateway.on_embed = assert_entries_committed
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {
                "drafts": [
                    {
                        "name": "银港",
                        "category": "地点",
                        "content": "银港终年被海雾笼罩。",
                    },
                    {
                        "name": "雾钟",
                        "category": "规则",
                        "content": "雾钟响起后禁止出港。",
                    },
                ]
            }
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        assert [entry.index_stale for entry in entries] == [False, False]
        assert gateway.embedding_request is not None
        assert gateway.embedding_request["expected_dimension"] == 3
        assert gateway.embedding_request["texts"] == [
            "名称：银港\n分类：地点\n关键词：银港\n内容：银港终年被海雾笼罩。",
            "名称：雾钟\n分类：规则\n关键词：雾钟\n内容：雾钟响起后禁止出港。",
        ]
        assert len(chroma.calls) == 1
        call = chroma.calls[0]
        assert call["ids"] == [
            worldbook_entry_document_id(entry.id) for entry in entries
        ]
        assert call["documents"] == gateway.embedding_request["texts"]
        assert call["metadatas"] == [
            {
                "sourceKind": "liveEntry",
                "entryId": entry.id,
                "worldBookId": book.id,
                "enabled": True,
                "resident": False,
                "embeddingVersion": 3,
                "contentHash": session.get(WorldBookEntryModel, entry.id).content_hash,
            }
            for entry in entries
        ]
        for entry in entries:
            model = session.get(WorldBookEntryModel, entry.id)
            assert model is not None
            assert model.index_status == "ready"
            assert model.embedding_version == 3
            assert model.last_index_error is None
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_keeps_entries_when_embedding_fails(tmp_path: Path) -> None:
    """Embedding 失败不回滚正式条目，并以失败索引状态返回。"""

    gateway = StubGateway(
        embedding_error=AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
    )
    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, gateway, chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session)
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {"drafts": [{"name": "银港", "category": "地点", "content": "原文事实"}]}
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        assert len(entries) == 1
        assert entries[0].index_stale is True
        model = session.get(WorldBookEntryModel, entries[0].id)
        assert model is not None
        assert model.index_status == "failed"
        assert model.embedding_version is None
        assert model.last_index_error == "无法连接模型服务"
        assert chroma.calls == []
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_keeps_entries_when_chroma_write_fails(tmp_path: Path) -> None:
    """Chroma 写入失败时保留 SQLite 条目并记录脱敏错误。"""

    service, session, engine = create_service(
        tmp_path,
        StubGateway(),
        RecordingChroma(fail=True),
    )
    try:
        book = service.create_world_book()
        configure_embedding(session)
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {"drafts": [{"name": "银港", "category": "地点", "content": "原文事实"}]}
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        assert entries[0].index_stale is True
        model = session.get(WorldBookEntryModel, entries[0].id)
        assert model is not None
        assert model.index_status == "failed"
        assert model.last_index_error == "世界书条目向量写入失败"
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_does_not_write_vector_after_embedding_version_changes(
    tmp_path: Path,
) -> None:
    """外部调用期间配置版本变化时，旧空间向量不能进入 Chroma。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, gateway, chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session, version=3)
        session_factory = create_session_factory(engine)

        def change_embedding_version() -> None:
            with session_factory() as other_session:
                config = other_session.get(ModelConfig, "embed")
                assert config is not None
                config.config_version = 4
                other_session.commit()

        gateway.on_embed = change_embedding_version
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {"drafts": [{"name": "银港", "category": "地点", "content": "原文事实"}]}
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        assert entries[0].index_stale is True
        assert chroma.calls == []
        model = session.get(WorldBookEntryModel, entries[0].id)
        assert model is not None
        assert model.index_status == "stale"
        assert model.embedding_version is None
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_does_not_mark_changed_content_ready(tmp_path: Path) -> None:
    """Embedding 期间正文哈希变化时，旧向量不能写入或标记为就绪。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, gateway, chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session, version=3)
        session_factory = create_session_factory(engine)

        def change_entry_content() -> None:
            with session_factory() as other_session:
                entry = other_session.scalar(
                    select(WorldBookEntryModel).where(
                        WorldBookEntryModel.world_book_id == book.id
                    )
                )
                assert entry is not None
                entry.content = "并发修改后的正文"
                entry.content_hash = calculate_content_hash(
                    build_worldbook_index_text(
                        name=entry.name,
                        category=entry.category,
                        keywords=json.loads(entry.keywords_json),
                        content=entry.content,
                    )
                )
                entry.index_status = "stale"
                other_session.commit()

        gateway.on_embed = change_entry_content
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {"drafts": [{"name": "银港", "category": "地点", "content": "原文事实"}]}
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        assert entries[0].content == "并发修改后的正文"
        assert entries[0].index_stale is True
        assert chroma.calls == []
        model = session.get(WorldBookEntryModel, entries[0].id)
        assert model is not None
        assert model.index_status == "stale"
        assert model.embedding_version is None
    finally:
        session.close()
        engine.dispose()


def test_confirm_drafts_writes_real_chroma_collection(tmp_path: Path) -> None:
    """确认入库可将正式条目真实写入物理隔离的世界书 Collection。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session = create_session_factory(engine)()
    chroma = ChromaRepository(settings)
    service = WorldBookService(session, StubGateway(), chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session, version=3)
        payload = ConfirmWorldBookDraftsPayload.model_validate(
            {"drafts": [{"name": "银港", "category": "地点", "content": "原文事实"}]}
        )

        entries = asyncio.run(service.confirm_drafts(book.id, payload))

        document_id = worldbook_entry_document_id(entries[0].id)
        collection = chroma.client.get_collection(
            WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        stored = collection.get(ids=[document_id], include=["documents", "metadatas"])
        assert stored["ids"] == [document_id]
        assert stored["documents"] == [
            "名称：银港\n分类：地点\n关键词：银港\n内容：原文事实"
        ]
        assert stored["metadatas"][0]["worldBookId"] == book.id
        assert stored["metadatas"][0]["embeddingVersion"] == 3
    finally:
        session.close()
        engine.dispose()


def test_reembed_only_processes_stale_entries_and_updates_metadata(
    tmp_path: Path,
) -> None:
    """单本重建仅处理过期条目，并同步启用与常驻 metadata。"""

    gateway = StubGateway()
    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, gateway, chroma)
    try:
        book = service.create_world_book()
        configure_embedding(session, version=3)
        first = service.create_entry(book.id)
        second = service.create_entry(book.id)
        second_model = session.get(WorldBookEntryModel, second.id)
        assert second_model is not None
        second_model.index_status = "ready"
        second_model.embedding_version = 3
        session.commit()

        updated = service.update_entry(
            book.id,
            first.id,
            UpdateWorldBookEntryPayload(enabled=False, resident=True),
        )
        assert updated.index_stale is True

        result = asyncio.run(service.reembed(book.id))

        assert result.count == 1
        assert gateway.embedding_request is not None
        assert len(gateway.embedding_request["texts"]) == 1
        assert chroma.calls[0]["ids"] == [worldbook_entry_document_id(first.id)]
        assert chroma.calls[0]["metadatas"][0]["enabled"] is False
        assert chroma.calls[0]["metadatas"][0]["resident"] is True
        assert service.get_world_book(book.id).entries[0].index_stale is False
        assert service.get_world_book(book.id).entries[1].index_stale is False
    finally:
        session.close()
        engine.dispose()


def test_reembed_preserves_failed_state_and_returns_model_error(tmp_path: Path) -> None:
    """显式重建失败时保留失败状态，并向 API 层继续抛出稳定错误。"""

    gateway = StubGateway(
        embedding_error=AppError(
            status_code=502,
            code="MODEL_CONNECTION_FAILED",
            message="无法连接模型服务",
        )
    )
    service, session, engine = create_service(tmp_path, gateway)
    try:
        book = service.create_world_book()
        configure_embedding(session)
        entry = service.create_entry(book.id)

        with pytest.raises(AppError) as error:
            asyncio.run(service.reembed(book.id))

        assert error.value.code == "MODEL_CONNECTION_FAILED"
        model = session.get(WorldBookEntryModel, entry.id)
        assert model is not None
        assert model.index_status == "failed"
        assert model.last_index_error == "无法连接模型服务"
    finally:
        session.close()
        engine.dispose()


def test_delete_entry_cleans_vector_after_sqlite_commit(tmp_path: Path) -> None:
    """条目删除成功后按确定 ID 清理 Chroma 派生向量。"""

    chroma = RecordingChroma()
    service, session, engine = create_service(tmp_path, chroma_repository=chroma)
    try:
        book = service.create_world_book()
        entry = service.create_entry(book.id)

        service.delete_entry(book.id, entry.id)

        assert session.get(WorldBookEntryModel, entry.id) is None
        assert chroma.deleted_ids == [worldbook_entry_document_id(entry.id)]
    finally:
        session.close()
        engine.dispose()


def test_delete_world_book_records_cleanup_tasks_when_chroma_fails(
    tmp_path: Path,
) -> None:
    """世界书事实删除不因 Chroma 失败回滚，并为每个孤立向量记录补偿任务。"""

    chroma = RecordingChroma(delete_fail=True)
    service, session, engine = create_service(tmp_path, chroma_repository=chroma)
    try:
        book = service.create_world_book()
        entries = [service.create_entry(book.id), service.create_entry(book.id)]

        service.delete_world_book(book.id)

        with pytest.raises(AppError) as error:
            service.get_world_book(book.id)
        assert error.value.status_code == 404
        tasks = list(
            session.scalars(
                select(BackgroundTask)
                .where(BackgroundTask.task_type == "vector_cleanup")
                .order_by(BackgroundTask.scope_id)
            ).all()
        )
        assert [task.scope_id for task in tasks] == sorted(
            worldbook_entry_document_id(entry.id) for entry in entries
        )
        assert all(task.status == "pending" for task in tasks)
        assert all(task.progress_total == 1 for task in tasks)
    finally:
        session.close()
        engine.dispose()
