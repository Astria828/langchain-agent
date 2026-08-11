"""按角色、有效状态和 Embedding 版本检索长期记忆。"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from app.db.models import LongTermMemory
from app.repositories.chroma_repository import (
    ChromaRepository,
    Embedding,
    calculate_content_hash,
    memory_document_id,
)

logger = logging.getLogger(__name__)

MEMORY_VECTOR_TOP_K = 3
MEMORY_MAX_COSINE_DISTANCE = 0.30


class MemoryRetriever:
    """只返回当前角色可验证的有效长期记忆。"""

    def __init__(self, session: DatabaseSession, chroma_repository: ChromaRepository) -> None:
        self.session = session
        self.chroma = chroma_repository

    def retrieve(
        self,
        *,
        character_id: str,
        query_embedding: Embedding | None,
        embedding_version: int,
    ) -> list[LongTermMemory]:
        """按向量顺序返回通过阈值和 SQLite 回查的角色级长期记忆。"""

        if query_embedding is None:
            return []

        ready_memories = {
            memory.id: memory
            for memory in self.session.scalars(
                select(LongTermMemory)
                .where(
                    LongTermMemory.character_id == character_id,
                    LongTermMemory.status == "active",
                    LongTermMemory.index_status == "ready",
                    LongTermMemory.embedding_version == embedding_version,
                )
                .order_by(LongTermMemory.id)
            ).all()
        }
        if not ready_memories:
            return []

        result = self.chroma.query_memories(
            query_embedding=query_embedding,
            n_results=MEMORY_VECTOR_TOP_K,
            where={
                "$and": [
                    {"characterId": character_id},
                    {"status": "active"},
                    {"embeddingVersion": embedding_version},
                ]
            },
        )

        retrieved: list[LongTermMemory] = []
        for document_id, distance, metadata in zip(
            result["ids"][0],
            result["distances"][0],
            result["metadatas"][0],
            strict=True,
        ):
            if distance > MEMORY_MAX_COSINE_DISTANCE:
                continue
            memory_id = metadata.get("memoryId")
            memory = ready_memories.get(memory_id)
            if memory is None or not self._metadata_matches_memory(
                document_id=document_id,
                metadata=metadata,
                memory=memory,
                character_id=character_id,
                embedding_version=embedding_version,
            ):
                logger.debug(
                    "丢弃与 SQLite 不一致的长期记忆向量 character_id=%s document_id=%s",
                    character_id,
                    document_id,
                )
                continue
            retrieved.append(memory)
        return retrieved

    @staticmethod
    def _metadata_matches_memory(
        *,
        document_id: str,
        metadata: dict[str, object],
        memory: LongTermMemory,
        character_id: str,
        embedding_version: int,
    ) -> bool:
        """确认确定 ID、角色、状态、类型、版本和正文哈希均一致。"""

        return (
            document_id == memory_document_id(memory.id)
            and memory.character_id == character_id
            and memory.status == "active"
            and memory.index_status == "ready"
            and memory.embedding_version == embedding_version
            and calculate_content_hash(memory.content) == memory.content_hash
            and metadata.get("characterId") == character_id
            and metadata.get("status") == "active"
            and metadata.get("memoryType") == memory.type
            and metadata.get("embeddingVersion") == embedding_version
            and metadata.get("contentHash") == memory.content_hash
        )
