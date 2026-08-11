"""阶段 6.2 世界书常驻、关键词与向量混合召回测试。"""

import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import (
    Character,
    ChatSession,
    SessionContextSnapshot,
    SessionWorldBookEntrySnapshot,
    WorldBook,
)
from app.repositories.chroma_repository import (
    build_worldbook_index_text,
    calculate_content_hash,
    session_entry_document_id,
)
from app.retrievers.worldbook_retriever import WorldBookRetriever
from sqlalchemy.orm import Session as DatabaseSession

from scripts.init_db import initialize_database


class RecordingChroma:
    """记录世界书查询参数并返回受控的 Chroma 结果。"""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"ids": [[]], "distances": [[]], "metadatas": [[]]}
        self.queries: list[dict[str, Any]] = []

    def query_worldbook(self, **kwargs) -> dict[str, Any]:
        """记录一次向量查询。"""

        self.queries.append(kwargs)
        return self.result


def create_database(tmp_path: Path):
    """创建使用隔离 SQLite 的 Retriever 测试数据库。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    return engine, create_session_factory(engine)()


def add_chat_session(
    session: DatabaseSession,
    *,
    bound_world_book: bool,
) -> tuple[ChatSession, SessionContextSnapshot]:
    """创建带可选世界书绑定和不可变上下文快照的会话。"""

    character = Character(name="艾拉")
    world_book = WorldBook(name="银港设定", raw_content="原文") if bound_world_book else None
    session.add(character)
    if world_book is not None:
        session.add(world_book)
    session.flush()

    chat_session = ChatSession(
        title="检索测试",
        character_id=character.id,
        world_book_id=world_book.id if world_book is not None else None,
        round_count=0,
        consolidated_round=0,
        summary="",
    )
    session.add(chat_session)
    session.flush()
    context = SessionContextSnapshot(
        session_id=chat_session.id,
        identity_source_id="identity-current",
        identity_name="用户",
        identity_persona_name="旅行者",
        identity_bio="",
        character_source_id=character.id,
        character_name=character.name,
        character_introduction="",
        character_system_prompt="",
        character_dialogue_examples_json="[]",
        world_book_source_id=world_book.id if world_book is not None else None,
        world_book_name=world_book.name if world_book is not None else None,
        world_book_raw_content=world_book.raw_content if world_book is not None else None,
    )
    session.add(context)
    session.commit()
    return chat_session, context


def add_snapshot(
    session: DatabaseSession,
    context: SessionContextSnapshot,
    *,
    position: int,
    name: str,
    keywords: list[str],
    resident: bool = False,
    index_status: str = "ready",
    embedding_version: int | None = 2,
) -> SessionWorldBookEntrySnapshot:
    """添加符合存储规范的会话世界书条目快照。"""

    content = f"{name}的设定。"
    index_text = build_worldbook_index_text(
        name=name,
        category="地点",
        keywords=keywords,
        content=content,
    )
    snapshot = SessionWorldBookEntrySnapshot(
        context_snapshot_id=context.id,
        source_entry_id=f"source-{context.id}-{position}",
        position=position,
        name=name,
        content=content,
        keywords_json=json.dumps(keywords, ensure_ascii=False),
        category="地点",
        resident=int(resident),
        index_status=index_status,
        embedding_version=embedding_version,
        content_hash=calculate_content_hash(index_text),
    )
    session.add(snapshot)
    session.commit()
    return snapshot


def vector_metadata(
    snapshot: SessionWorldBookEntrySnapshot,
    chat_session: ChatSession,
    *,
    content_hash: str | None = None,
) -> dict[str, object]:
    """构造会话快照向量的标准 metadata。"""

    return {
        "sourceKind": "sessionSnapshot",
        "entrySnapshotId": snapshot.id,
        "sessionId": chat_session.id,
        "worldBookId": chat_session.world_book_id,
        "resident": bool(snapshot.resident),
        "embeddingVersion": snapshot.embedding_version,
        "contentHash": content_hash or snapshot.content_hash,
    }


def test_unbound_session_short_circuits_before_chroma_query(tmp_path: Path) -> None:
    """未绑定世界书时直接返回空结果且不调用 Chroma。"""

    engine, session = create_database(tmp_path)
    try:
        chat_session, _context = add_chat_session(session, bound_world_book=False)
        chroma = RecordingChroma()

        result = WorldBookRetriever(session, chroma).retrieve(
            chat_session=chat_session,
            current_input="银港在哪里？",
            query_embedding=[1.0, 0.0, 0.0],
            embedding_version=2,
        )

        assert result == []
        assert chroma.queries == []
    finally:
        session.close()
        engine.dispose()


def test_mixed_retrieval_merges_resident_keyword_and_vector_in_order(
    tmp_path: Path,
) -> None:
    """混合召回按固定优先级去重，并执行 Top 3 与余弦距离阈值。"""

    engine, session = create_database(tmp_path)
    try:
        chat_session, context = add_chat_session(session, bound_world_book=True)
        resident = add_snapshot(
            session,
            context,
            position=0,
            name="学院规则",
            keywords=["学院"],
            resident=True,
        )
        keyword = add_snapshot(
            session,
            context,
            position=1,
            name="银港",
            keywords=["Silver Port"],
        )
        vector = add_snapshot(
            session,
            context,
            position=2,
            name="旧塔",
            keywords=["旧塔"],
        )
        below_threshold = add_snapshot(
            session,
            context,
            position=3,
            name="北境",
            keywords=["北境"],
        )
        chroma = RecordingChroma(
            {
                "ids": [[
                    session_entry_document_id(keyword.id),
                    session_entry_document_id(vector.id),
                    session_entry_document_id(below_threshold.id),
                ]],
                "distances": [[0.05, 0.40, 0.400001]],
                "metadatas": [[
                    vector_metadata(keyword, chat_session),
                    vector_metadata(vector, chat_session),
                    vector_metadata(below_threshold, chat_session),
                ]],
            }
        )

        result = WorldBookRetriever(session, chroma).retrieve(
            chat_session=chat_session,
            current_input="我们去 SILVER PORT 看看。",
            query_embedding=[0.1, 0.2, 0.3],
            embedding_version=2,
        )

        assert [snapshot.id for snapshot in result] == [resident.id, keyword.id, vector.id]
        assert chroma.queries == [
            {
                "query_embedding": [0.1, 0.2, 0.3],
                "n_results": 3,
                "where": {
                    "$and": [
                        {"sourceKind": "sessionSnapshot"},
                        {"sessionId": chat_session.id},
                        {"embeddingVersion": 2},
                    ]
                },
            }
        ]
    finally:
        session.close()
        engine.dispose()


def test_vector_results_must_match_current_sqlite_snapshot(tmp_path: Path) -> None:
    """旧状态、错误哈希和其他会话的残留向量均不能被采用。"""

    engine, session = create_database(tmp_path)
    try:
        chat_session, context = add_chat_session(session, bound_world_book=True)
        hash_mismatch = add_snapshot(
            session,
            context,
            position=0,
            name="错误哈希",
            keywords=[],
        )
        stale = add_snapshot(
            session,
            context,
            position=1,
            name="旧版本",
            keywords=[],
            index_status="stale",
            embedding_version=1,
        )
        other_session, other_context = add_chat_session(session, bound_world_book=True)
        foreign = add_snapshot(
            session,
            other_context,
            position=0,
            name="其他会话",
            keywords=[],
        )
        chroma = RecordingChroma(
            {
                "ids": [[
                    session_entry_document_id(hash_mismatch.id),
                    session_entry_document_id(stale.id),
                    session_entry_document_id(foreign.id),
                ]],
                "distances": [[0.10, 0.10, 0.10]],
                "metadatas": [[
                    vector_metadata(hash_mismatch, chat_session, content_hash="wrong-hash"),
                    vector_metadata(stale, chat_session),
                    vector_metadata(foreign, other_session),
                ]],
            }
        )

        result = WorldBookRetriever(session, chroma).retrieve(
            chat_session=chat_session,
            current_input="没有关键词",
            query_embedding=[0.1, 0.2, 0.3],
            embedding_version=2,
        )

        assert result == []
    finally:
        session.close()
        engine.dispose()


def test_missing_query_embedding_keeps_resident_and_keyword_results(
    tmp_path: Path,
) -> None:
    """没有查询向量时跳过 Chroma，但保留不依赖向量的召回结果。"""

    engine, session = create_database(tmp_path)
    try:
        chat_session, context = add_chat_session(session, bound_world_book=True)
        resident = add_snapshot(
            session,
            context,
            position=0,
            name="常驻规则",
            keywords=[],
            resident=True,
            index_status="failed",
            embedding_version=None,
        )
        keyword = add_snapshot(
            session,
            context,
            position=1,
            name="银港",
            keywords=["银港"],
            index_status="failed",
            embedding_version=None,
        )
        chroma = RecordingChroma()

        result = WorldBookRetriever(session, chroma).retrieve(
            chat_session=chat_session,
            current_input="我要去银港。",
            query_embedding=None,
            embedding_version=2,
        )

        assert [snapshot.id for snapshot in result] == [resident.id, keyword.id]
        assert chroma.queries == []
    finally:
        session.close()
        engine.dispose()
