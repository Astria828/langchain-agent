"""物理隔离的世界书与长期记忆 Chroma Collection 基础读写。"""

from hashlib import sha256
from typing import Any

import chromadb

from app.core.config import Settings, get_settings

WORLD_BOOK_COLLECTION = "worldbook_entries"
LONG_TERM_MEMORY_COLLECTION = "long_term_memories"
COLLECTION_CONFIGURATION = {"hnsw": {"space": "cosine"}}

Metadata = dict[str, str | int | float | bool]
Embedding = list[float]


def build_worldbook_index_text(
    *,
    name: str,
    category: str,
    keywords: list[str],
    content: str,
) -> str:
    """按存储规范生成稳定的世界书索引文本。"""

    return f"名称：{name}\n分类：{category}\n关键词：{'、'.join(keywords)}\n内容：{content}"


def calculate_content_hash(index_text: str) -> str:
    """计算索引文本的 UTF-8 SHA-256。"""

    return sha256(index_text.encode("utf-8")).hexdigest()


def worldbook_entry_document_id(entry_id: str) -> str:
    """生成正式世界书条目的确定文档 ID。"""

    return f"worldbook-entry:{entry_id}"


def session_entry_document_id(entry_snapshot_id: str) -> str:
    """生成会话条目快照的确定文档 ID。"""

    return f"session-entry:{entry_snapshot_id}"


def memory_document_id(memory_id: str) -> str:
    """生成长期记忆的确定文档 ID。"""

    return f"memory:{memory_id}"


class ChromaRepository:
    """通过固定 Collection 名称防止世界书与长期记忆混写。"""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved_settings = settings or get_settings()
        resolved_settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(resolved_settings.chroma_dir))

    def initialize_collections(self) -> tuple[str, str]:
        """幂等创建两个不使用 Chroma 默认 Embedding 的 Collection。"""

        self.client.get_or_create_collection(
            name=WORLD_BOOK_COLLECTION,
            configuration=COLLECTION_CONFIGURATION,
            embedding_function=None,
        )
        self.client.get_or_create_collection(
            name=LONG_TERM_MEMORY_COLLECTION,
            configuration=COLLECTION_CONFIGURATION,
            embedding_function=None,
        )
        return WORLD_BOOK_COLLECTION, LONG_TERM_MEMORY_COLLECTION

    def reset_collections(self) -> tuple[str, str]:
        """删除并重建两个纯派生 Collection，清除旧版本和孤立向量。"""

        self.initialize_collections()
        self.client.delete_collection(name=WORLD_BOOK_COLLECTION)
        self.client.delete_collection(name=LONG_TERM_MEMORY_COLLECTION)
        return self.initialize_collections()

    def upsert_worldbook(
        self,
        *,
        ids: list[str],
        embeddings: list[Embedding],
        documents: list[str],
        metadatas: list[Metadata],
    ) -> None:
        """只向世界书 Collection 写入正式条目或会话快照。"""

        collection = self.client.get_collection(
            name=WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def upsert_memories(
        self,
        *,
        ids: list[str],
        embeddings: list[Embedding],
        documents: list[str],
        metadatas: list[Metadata],
    ) -> None:
        """只向长期记忆 Collection 写入长期记忆。"""

        collection = self.client.get_collection(
            name=LONG_TERM_MEMORY_COLLECTION,
            embedding_function=None,
        )
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def query_worldbook(
        self,
        *,
        query_embedding: Embedding,
        n_results: int,
        where: dict[str, Any],
    ) -> dict[str, Any]:
        """在世界书 Collection 内按会话等 metadata 边界检索。"""

        collection = self.client.get_collection(
            name=WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        return collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
        )

    def query_memories(
        self,
        *,
        query_embedding: Embedding,
        n_results: int,
        where: dict[str, Any],
    ) -> dict[str, Any]:
        """在长期记忆 Collection 内按角色、状态和版本检索。"""

        collection = self.client.get_collection(
            name=LONG_TERM_MEMORY_COLLECTION,
            embedding_function=None,
        )
        return collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=where,
        )

    def delete_worldbook(self, *, ids: list[str]) -> None:
        """按确定 ID 删除世界书派生向量。"""

        collection = self.client.get_collection(
            name=WORLD_BOOK_COLLECTION,
            embedding_function=None,
        )
        collection.delete(ids=ids)

    def delete_memories(self, *, ids: list[str]) -> None:
        """按确定 ID 删除长期记忆派生向量。"""

        collection = self.client.get_collection(
            name=LONG_TERM_MEMORY_COLLECTION,
            embedding_function=None,
        )
        collection.delete(ids=ids)
