"""阶段 1B 的 SQLite 与双 Chroma Repository 测试。"""

from pathlib import Path

from app.core.config import Settings
from app.db.database import create_database_engine, create_session_factory
from app.db.models import Character
from app.repositories.chroma_repository import (
    LONG_TERM_MEMORY_COLLECTION,
    WORLD_BOOK_COLLECTION,
    ChromaRepository,
    build_worldbook_index_text,
    calculate_content_hash,
    memory_document_id,
    session_entry_document_id,
)
from app.repositories.sqlite_repository import SQLiteRepository

from scripts.init_db import initialize_database


def isolated_settings(tmp_path: Path) -> Settings:
    """构造隔离的 SQLite 与 Chroma 测试目录。"""

    return Settings(environment="test", data_dir=tmp_path)


def test_sqlite_repository_flushes_without_committing(tmp_path: Path) -> None:
    """Repository 只 flush，事务提交与回滚由调用方决定。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        repository = SQLiteRepository(session)
        character = repository.add(Character(name="角色一"))
        repository.flush()
        character_id = character.id
        assert repository.get(Character, character_id) is character
        session.rollback()

    with session_factory() as session:
        assert SQLiteRepository(session).get(Character, character_id) is None

    engine.dispose()


def test_sqlite_repository_lists_in_explicit_order(tmp_path: Path) -> None:
    """MVP 列表按调用方指定顺序一次返回全部数据。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)

    with session_factory.begin() as session:
        repository = SQLiteRepository(session)
        repository.add(Character(name="较早", updated_at="2026-01-01T00:00:00Z"))
        repository.add(Character(name="较晚", updated_at="2026-02-01T00:00:00Z"))

    with session_factory() as session:
        characters = SQLiteRepository(session).list_all(
            Character,
            order_by=(Character.updated_at.desc(),),
        )
        assert [character.name for character in characters] == ["较晚", "较早"]

    engine.dispose()


def test_chroma_collections_are_initialized_and_physically_isolated(
    tmp_path: Path,
) -> None:
    """世界书和长期记忆只能写入各自固定 Collection。"""

    settings = isolated_settings(tmp_path)
    initialize_database(settings)
    repository = ChromaRepository(settings)
    collection_names = {
        collection.name for collection in repository.client.list_collections()
    }
    assert collection_names == {WORLD_BOOK_COLLECTION, LONG_TERM_MEMORY_COLLECTION}

    worldbook_id = session_entry_document_id("snapshot-1")
    memory_id = memory_document_id("memory-1")
    repository.upsert_worldbook(
        ids=[worldbook_id],
        embeddings=[[1.0, 0.0, 0.0]],
        documents=["世界书内容"],
        metadatas=[
            {
                "sourceKind": "sessionSnapshot",
                "entrySnapshotId": "snapshot-1",
                "sessionId": "session-1",
                "worldBookId": "book-1",
                "resident": False,
                "embeddingVersion": 1,
                "contentHash": "hash-worldbook",
            }
        ],
    )
    repository.upsert_memories(
        ids=[memory_id],
        embeddings=[[0.0, 1.0, 0.0]],
        documents=["长期记忆内容"],
        metadatas=[
            {
                "memoryId": "memory-1",
                "characterId": "character-1",
                "status": "active",
                "memoryType": "重要剧情",
                "embeddingVersion": 1,
                "contentHash": "hash-memory",
            }
        ],
    )

    worldbook_collection = repository.client.get_collection(
        WORLD_BOOK_COLLECTION,
        embedding_function=None,
    )
    memory_collection = repository.client.get_collection(
        LONG_TERM_MEMORY_COLLECTION,
        embedding_function=None,
    )
    assert worldbook_collection.configuration_json["hnsw"]["space"] == "cosine"
    assert memory_collection.configuration_json["hnsw"]["space"] == "cosine"
    assert worldbook_collection.get()["ids"] == [worldbook_id]
    assert memory_collection.get()["ids"] == [memory_id]

    worldbook_result = repository.query_worldbook(
        query_embedding=[1.0, 0.0, 0.0],
        n_results=1,
        where={"sessionId": "session-1"},
    )
    memory_result = repository.query_memories(
        query_embedding=[0.0, 1.0, 0.0],
        n_results=1,
        where={
            "$and": [
                {"characterId": "character-1"},
                {"status": "active"},
                {"embeddingVersion": 1},
            ]
        },
    )
    assert worldbook_result["ids"] == [[worldbook_id]]
    assert memory_result["ids"] == [[memory_id]]

    repository.delete_worldbook(ids=[worldbook_id])
    repository.delete_memories(ids=[memory_id])
    assert worldbook_collection.count() == 0
    assert memory_collection.count() == 0

    repository.reset_collections()
    rebuilt_worldbook = repository.client.get_collection(
        WORLD_BOOK_COLLECTION,
        embedding_function=None,
    )
    rebuilt_memory = repository.client.get_collection(
        LONG_TERM_MEMORY_COLLECTION,
        embedding_function=None,
    )
    assert rebuilt_worldbook.configuration_json["hnsw"]["space"] == "cosine"
    assert rebuilt_memory.configuration_json["hnsw"]["space"] == "cosine"


def test_worldbook_index_text_and_hash_follow_storage_specification() -> None:
    """索引文本和哈希使用文档规定的稳定拼接规则。"""

    index_text = build_worldbook_index_text(
        name="星港",
        category="地点",
        keywords=["港口", "航线"],
        content="一座长期停泊的空间站。",
    )

    assert index_text == (
        "名称：星港\n分类：地点\n关键词：港口、航线\n内容：一座长期停泊的空间站。"
    )
    assert calculate_content_hash(index_text) == (
        "90680016f087546b074a63251911fc28df04ec6f2dd9067186085279adbb5f04"
    )
