"""基于会话世界书快照的常驻、关键词与向量混合召回。"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from app.db.models import (
    ChatSession,
    SessionContextSnapshot,
    SessionWorldBookEntrySnapshot,
)
from app.repositories.chroma_repository import (
    ChromaRepository,
    Embedding,
    session_entry_document_id,
)

logger = logging.getLogger(__name__)

WORLD_BOOK_VECTOR_TOP_K = 3
WORLD_BOOK_MAX_COSINE_DISTANCE = 0.40


class WorldBookRetriever:
    """只从当前会话冻结的世界书条目快照中执行混合召回。"""

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
    ) -> list[SessionWorldBookEntrySnapshot]:
        """按常驻、关键词、向量顺序返回去重后的当前会话条目快照。"""

        if chat_session.world_book_id is None:
            return []

        snapshots = self._load_session_snapshots(chat_session.id)
        if not snapshots:
            return []

        resident = [snapshot for snapshot in snapshots if bool(snapshot.resident)]
        keyword = self._match_keywords(snapshots, current_input)
        vector = self._retrieve_vectors(
            chat_session=chat_session,
            snapshots=snapshots,
            query_embedding=query_embedding,
            embedding_version=embedding_version,
        )

        merged: list[SessionWorldBookEntrySnapshot] = []
        seen_ids: set[str] = set()
        for snapshot in (*resident, *keyword, *vector):
            if snapshot.id not in seen_ids:
                seen_ids.add(snapshot.id)
                merged.append(snapshot)
        return merged

    def _load_session_snapshots(
        self,
        session_id: str,
    ) -> list[SessionWorldBookEntrySnapshot]:
        """按冻结位置读取当前会话创建时复制的已启用条目。"""

        return list(
            self.session.scalars(
                select(SessionWorldBookEntrySnapshot)
                .join(
                    SessionContextSnapshot,
                    SessionContextSnapshot.id
                    == SessionWorldBookEntrySnapshot.context_snapshot_id,
                )
                .where(SessionContextSnapshot.session_id == session_id)
                .order_by(
                    SessionWorldBookEntrySnapshot.position,
                    SessionWorldBookEntrySnapshot.id,
                )
            ).all()
        )

    @staticmethod
    def _match_keywords(
        snapshots: list[SessionWorldBookEntrySnapshot],
        current_input: str,
    ) -> list[SessionWorldBookEntrySnapshot]:
        """在当前输入中执行忽略英文大小写的稳定子串匹配。"""

        normalized_input = current_input.casefold()
        matched: list[SessionWorldBookEntrySnapshot] = []
        for snapshot in snapshots:
            keywords = json.loads(snapshot.keywords_json)
            if any(
                keyword.strip() and keyword.strip().casefold() in normalized_input
                for keyword in keywords
            ):
                matched.append(snapshot)
        return matched

    def _retrieve_vectors(
        self,
        *,
        chat_session: ChatSession,
        snapshots: list[SessionWorldBookEntrySnapshot],
        query_embedding: Embedding | None,
        embedding_version: int,
    ) -> list[SessionWorldBookEntrySnapshot]:
        """查询当前会话向量并用 SQLite 状态与哈希回查结果。"""

        if query_embedding is None:
            return []

        ready_snapshots = {
            snapshot.id: snapshot
            for snapshot in snapshots
            if snapshot.index_status == "ready"
            and snapshot.embedding_version == embedding_version
        }
        if not ready_snapshots:
            return []

        result = self.chroma.query_worldbook(
            query_embedding=query_embedding,
            n_results=WORLD_BOOK_VECTOR_TOP_K,
            where={
                "$and": [
                    {"sourceKind": "sessionSnapshot"},
                    {"sessionId": chat_session.id},
                    {"embeddingVersion": embedding_version},
                ]
            },
        )

        retrieved: list[SessionWorldBookEntrySnapshot] = []
        for document_id, distance, metadata in zip(
            result["ids"][0],
            result["distances"][0],
            result["metadatas"][0],
            strict=True,
        ):
            if distance > WORLD_BOOK_MAX_COSINE_DISTANCE:
                continue
            snapshot_id = metadata.get("entrySnapshotId")
            snapshot = ready_snapshots.get(snapshot_id)
            if snapshot is None or not self._metadata_matches_snapshot(
                document_id=document_id,
                metadata=metadata,
                snapshot=snapshot,
                chat_session=chat_session,
                embedding_version=embedding_version,
            ):
                logger.debug(
                    "丢弃与 SQLite 不一致的世界书向量 session_id=%s document_id=%s",
                    chat_session.id,
                    document_id,
                )
                continue
            retrieved.append(snapshot)
        return retrieved

    @staticmethod
    def _metadata_matches_snapshot(
        *,
        document_id: str,
        metadata: dict[str, object],
        snapshot: SessionWorldBookEntrySnapshot,
        chat_session: ChatSession,
        embedding_version: int,
    ) -> bool:
        """确认确定 ID、会话边界、版本和内容哈希均与 SQLite 一致。"""

        return (
            document_id == session_entry_document_id(snapshot.id)
            and metadata.get("sourceKind") == "sessionSnapshot"
            and metadata.get("sessionId") == chat_session.id
            and metadata.get("worldBookId") == chat_session.world_book_id
            and metadata.get("embeddingVersion") == embedding_version
            and metadata.get("contentHash") == snapshot.content_hash
        )
