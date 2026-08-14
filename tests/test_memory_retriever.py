"""阶段 6.3 长期记忆角色隔离、阈值和一致性回查测试。"""

from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import Character, LongTermMemory
from app.repositories.chroma_repository import (
    calculate_content_hash,
    memory_document_id,
)
from app.retrievers.memory_retriever import MemoryRetriever
from sqlalchemy.orm import Session as DatabaseSession

from scripts.init_db import initialize_database


class RecordingChroma:
    """记录长期记忆查询参数并返回受控的 Chroma 结果。"""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"ids": [[]], "distances": [[]], "metadatas": [[]]}
        self.queries: list[dict[str, Any]] = []

    def query_memories(self, **kwargs) -> dict[str, Any]:
        """记录一次长期记忆向量查询。"""

        self.queries.append(kwargs)
        return self.result


def create_database(tmp_path: Path):
    """创建使用隔离 SQLite 的长期记忆 Retriever 测试数据库。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    return engine, create_session_factory(engine)()


def add_character(session: DatabaseSession, name: str) -> Character:
    """创建长期记忆所属角色。"""

    character = Character(name=name)
    session.add(character)
    session.commit()
    return character


def add_memory(
    session: DatabaseSession,
    character: Character,
    *,
    content: str,
    memory_type: str = "重要剧情",
    status: str = "active",
    index_status: str = "ready",
    embedding_version: int | None = 2,
    content_hash: str | None = None,
) -> LongTermMemory:
    """添加带明确索引状态和正文哈希的长期记忆。"""

    memory = LongTermMemory(
        character_id=character.id,
        type=memory_type,
        content=content,
        importance=3,
        status=status,
        source_label="测试对话",
        memory_version=1,
        index_status=index_status,
        embedding_version=embedding_version,
        content_hash=content_hash or calculate_content_hash(content),
    )
    session.add(memory)
    session.commit()
    return memory


def vector_metadata(
    memory: LongTermMemory,
    *,
    character_id: str | None = None,
    status: str | None = None,
) -> dict[str, object]:
    """构造长期记忆向量的标准 metadata。"""

    return {
        "memoryId": memory.id,
        "characterId": character_id or memory.character_id,
        "status": status or memory.status,
        "memoryType": memory.type,
        "embeddingVersion": memory.embedding_version,
        "contentHash": memory.content_hash,
    }


def test_memory_retrieval_applies_role_filter_top_k_and_cosine_threshold(
    tmp_path: Path,
) -> None:
    """只查询当前角色有效版本，并保留余弦距离不超过 0.30 的 Top 3。"""

    engine, session = create_database(tmp_path)
    try:
        character = add_character(session, "艾拉")
        first = add_memory(
            session, character, content="用户喜欢胶片相机。", memory_type="用户偏好"
        )
        edge = add_memory(
            session, character, content="艾拉答应一起看海。", memory_type="角色承诺"
        )
        below_threshold = add_memory(session, character, content="曾经路过北境。")
        chroma = RecordingChroma(
            {
                "ids": [
                    [
                        memory_document_id(first.id),
                        memory_document_id(edge.id),
                        memory_document_id(below_threshold.id),
                    ]
                ],
                "distances": [[0.05, 0.30, 0.300001]],
                "metadatas": [
                    [
                        vector_metadata(first),
                        vector_metadata(edge),
                        vector_metadata(below_threshold),
                    ]
                ],
            }
        )

        result = MemoryRetriever(session, chroma).retrieve(
            character_id=character.id,
            query_embedding=[0.1, 0.2, 0.3],
            embedding_version=2,
        )

        assert [memory.id for memory in result] == [first.id, edge.id]
        assert chroma.queries == [
            {
                "query_embedding": [0.1, 0.2, 0.3],
                "n_results": 3,
                "where": {
                    "$and": [
                        {"characterId": character.id},
                        {"status": "active"},
                        {"embeddingVersion": 2},
                    ]
                },
            }
        ]
    finally:
        session.close()
        engine.dispose()


def test_memory_results_must_match_current_sqlite_record(tmp_path: Path) -> None:
    """正文哈希、有效状态或角色不一致的残留向量均不能被采用。"""

    engine, session = create_database(tmp_path)
    try:
        character = add_character(session, "艾拉")
        other_character = add_character(session, "诺亚")
        stale_hash = add_memory(
            session,
            character,
            content="正文已经变化。",
            content_hash="stale-content-hash",
        )
        invalid = add_memory(
            session,
            character,
            content="已经失效的承诺。",
            status="invalid",
        )
        foreign = add_memory(session, other_character, content="其他角色的经历。")
        chroma = RecordingChroma(
            {
                "ids": [
                    [
                        memory_document_id(stale_hash.id),
                        memory_document_id(invalid.id),
                        memory_document_id(foreign.id),
                    ]
                ],
                "distances": [[0.10, 0.10, 0.10]],
                "metadatas": [
                    [
                        vector_metadata(stale_hash),
                        vector_metadata(invalid, status="active"),
                        vector_metadata(foreign),
                    ]
                ],
            }
        )

        result = MemoryRetriever(session, chroma).retrieve(
            character_id=character.id,
            query_embedding=[0.1, 0.2, 0.3],
            embedding_version=2,
        )

        assert result == []
    finally:
        session.close()
        engine.dispose()


def test_memory_retrieval_skips_chroma_without_embedding_or_eligible_records(
    tmp_path: Path,
) -> None:
    """查询向量缺失或当前角色没有就绪记忆时不调用 Chroma。"""

    engine, session = create_database(tmp_path)
    try:
        character = add_character(session, "艾拉")
        old_version = add_memory(
            session,
            character,
            content="旧模型生成的记忆。",
            embedding_version=1,
        )
        chroma = RecordingChroma()
        retriever = MemoryRetriever(session, chroma)

        without_embedding = retriever.retrieve(
            character_id=character.id,
            query_embedding=None,
            embedding_version=2,
        )
        without_current_version = retriever.retrieve(
            character_id=character.id,
            query_embedding=[0.1, 0.2, 0.3],
            embedding_version=2,
        )

        assert old_version.embedding_version == 1
        assert without_embedding == []
        assert without_current_version == []
        assert chroma.queries == []
    finally:
        session.close()
        engine.dispose()
