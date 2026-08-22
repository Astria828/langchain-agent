"""基于世界书正式条目的常驻、关键词与向量混合召回。"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from app.db.models import ChatSession, WorldBookEntry
from app.repositories.chroma_repository import (
    ChromaRepository,
    Embedding,
    worldbook_entry_document_id,
)

logger = logging.getLogger(__name__)

WORLD_BOOK_VECTOR_TOP_K = 3
WORLD_BOOK_MAX_COSINE_DISTANCE = 0.40


class WorldBookRetriever:
    """从会话当前绑定的世界书正式条目中执行混合召回。"""

    def __init__(self, session: DatabaseSession, chroma_repository: ChromaRepository) -> None:
        self.session = session
        self.chroma = chroma_repository

    def retrieve(
        self,
        *,
        chat_session: ChatSession,
        current_input: str,
        query_embedding: Embedding | None,
        embedding_version: int,
    ) -> list[WorldBookEntry]:
        """按常驻、关键词、向量顺序返回去重后的已启用条目。"""

        if chat_session.world_book_id is None:
            return []

        entries = self._load_enabled_entries(chat_session.world_book_id)
        if not entries:
            return []

        resident = [entry for entry in entries if bool(entry.resident)]
        keyword = self._match_keywords(entries, current_input)
        vector = self._retrieve_vectors(
            chat_session=chat_session,
            entries=entries,
            query_embedding=query_embedding,
            embedding_version=embedding_version,
        )

        merged: list[WorldBookEntry] = []
        seen_ids: set[str] = set()
        for entry in (*resident, *keyword, *vector):
            if entry.id not in seen_ids:
                seen_ids.add(entry.id)
                merged.append(entry)
        return merged

    def _load_enabled_entries(self, world_book_id: str) -> list[WorldBookEntry]:
        """按条目顺序读取世界书当前已启用的正式条目。"""

        return list(
            self.session.scalars(
                select(WorldBookEntry)
                .where(
                    WorldBookEntry.world_book_id == world_book_id,
                    WorldBookEntry.enabled == 1,
                )
                .order_by(WorldBookEntry.position, WorldBookEntry.id)
            ).all()
        )

    @staticmethod
    def _match_keywords(
        entries: list[WorldBookEntry],
        current_input: str,
    ) -> list[WorldBookEntry]:
        """在当前输入中执行忽略英文大小写的稳定子串匹配。"""

        normalized_input = current_input.casefold()
        matched: list[WorldBookEntry] = []
        for entry in entries:
            keywords = json.loads(entry.keywords_json)
            if any(
                keyword.strip() and keyword.strip().casefold() in normalized_input
                for keyword in keywords
            ):
                matched.append(entry)
        return matched

    def _retrieve_vectors(
        self,
        *,
        chat_session: ChatSession,
        entries: list[WorldBookEntry],
        query_embedding: Embedding | None,
        embedding_version: int,
    ) -> list[WorldBookEntry]:
        """查询正式条目向量并用 SQLite 状态与哈希回查结果。"""

        if query_embedding is None:
            return []

        ready_entries = {
            entry.id: entry
            for entry in entries
            if entry.index_status == "ready" and entry.embedding_version == embedding_version
        }
        if not ready_entries:
            return []

        result = self.chroma.query_worldbook(
            query_embedding=query_embedding,
            n_results=WORLD_BOOK_VECTOR_TOP_K,
            where={
                "$and": [
                    {"sourceKind": "liveEntry"},
                    {"worldBookId": chat_session.world_book_id},
                    {"embeddingVersion": embedding_version},
                ]
            },
        )

        retrieved: list[WorldBookEntry] = []
        for document_id, distance, metadata in zip(
            result["ids"][0],
            result["distances"][0],
            result["metadatas"][0],
            strict=True,
        ):
            if distance > WORLD_BOOK_MAX_COSINE_DISTANCE:
                continue
            entry_id = metadata.get("entryId")
            entry = ready_entries.get(entry_id)
            if entry is None or not self._metadata_matches_entry(
                document_id=document_id,
                metadata=metadata,
                entry=entry,
                chat_session=chat_session,
                embedding_version=embedding_version,
            ):
                logger.debug(
                    "丢弃与 SQLite 不一致的世界书向量 session_id=%s document_id=%s",
                    chat_session.id,
                    document_id,
                )
                continue
            retrieved.append(entry)
        return retrieved

    @staticmethod
    def _metadata_matches_entry(
        *,
        document_id: str,
        metadata: dict[str, object],
        entry: WorldBookEntry,
        chat_session: ChatSession,
        embedding_version: int,
    ) -> bool:
        """确认确定 ID、世界书边界、启用状态、版本和内容哈希均与 SQLite 一致。"""

        return (
            document_id == worldbook_entry_document_id(entry.id)
            and metadata.get("sourceKind") == "liveEntry"
            and metadata.get("worldBookId") == chat_session.world_book_id
            and metadata.get("enabled") is True
            and metadata.get("embeddingVersion") == embedding_version
            and metadata.get("contentHash") == entry.content_hash
        )
