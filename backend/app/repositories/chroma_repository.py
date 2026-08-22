"""物理隔离的世界书与长期记忆 Chroma Collection 基础读写。"""

from hashlib import sha256
from pathlib import Path
from threading import RLock
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


def memory_document_id(memory_id: str) -> str:
    """生成长期记忆的确定文档 ID。"""

    return f"memory:{memory_id}"


_client_lock = RLock()
_clients: dict[str, chromadb.api.ClientAPI] = {}


def _shared_client(chroma_dir: Path) -> chromadb.api.ClientAPI:
    """按数据目录返回进程内共享的 Chroma 客户端。

    chromadb 自己的 SharedSystemClient 注册表不是线程安全的：FastAPI 把同步依赖
    丢进线程池，请求与后台任务并发构造同一路径的 PersistentClient 时会互相踩到
    对方的 system 注册/释放，抛出 "Could not connect to tenant default_tenant"。
    这里统一加锁并复用同一个客户端，同时省掉每次构造的开销。
    """

    path = str(chroma_dir)
    with _client_lock:
        client = _clients.get(path)
        if client is None:
            chroma_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=path)
            _clients[path] = client
        return client


class ChromaRepository:
    """通过固定 Collection 名称防止世界书与长期记忆混写。"""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved_settings = settings or get_settings()
        self.client = _shared_client(resolved_settings.chroma_dir)

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
        """只向世界书 Collection 写入正式条目。"""

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
