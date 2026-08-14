"""阶段 1A 的 SQLite 事实数据模型。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    """生成不可复用的 UUID 字符串主键。"""

    return str(uuid4())


def utc_now() -> str:
    """生成带 Z 后缀的 ISO 8601 UTC 时间。"""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    """全部 SQLAlchemy 模型的声明式基类。"""


class UserIdentity(Base):
    """单用户当前身份。"""

    __tablename__ = "user_identities"
    __table_args__ = (
        CheckConstraint("singleton_key = 'current'", name="ck_user_identities_singleton_key"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    singleton_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    persona_name: Mapped[str] = mapped_column(Text, nullable=False)
    bio: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class Character(Base):
    """角色卡及其有序对话示例。"""

    __tablename__ = "characters"
    __table_args__ = (Index("ix_characters_updated_at", "updated_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    introduction: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    dialogue_examples_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class WorldBook(Base):
    """保留用户完整原文的世界书。"""

    __tablename__ = "world_books"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class WorldBookEntry(Base):
    """世界书正式条目及其可重建索引状态。"""

    __tablename__ = "world_book_entries"
    __table_args__ = (
        CheckConstraint("resident IN (0, 1)", name="ck_world_book_entries_resident"),
        CheckConstraint("enabled IN (0, 1)", name="ck_world_book_entries_enabled"),
        CheckConstraint(
            "index_status IN ('pending', 'ready', 'stale', 'failed')",
            name="ck_world_book_entries_index_status",
        ),
        Index("ux_world_book_entries_position", "world_book_id", "position", unique=True),
        Index(
            "ix_world_book_entries_lookup",
            "world_book_id",
            "enabled",
            "resident",
            "position",
        ),
        Index("ix_world_book_entries_index_status", "index_status", "embedding_version"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    world_book_id: Mapped[str] = mapped_column(
        Text, ForeignKey("world_books.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'未分类'"))
    resident: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    index_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    embedding_version: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class ChatSession(Base):
    """绑定角色与可选世界书的对话会话。"""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("round_count >= 0", name="ck_sessions_round_count"),
        CheckConstraint("consolidated_round >= 0", name="ck_sessions_consolidated_round"),
        CheckConstraint(
            "consolidated_round <= round_count", name="ck_sessions_consolidated_not_ahead"
        ),
        Index("ix_sessions_updated_at", "updated_at"),
        Index("ix_sessions_character", "character_id", "updated_at"),
        Index("ix_sessions_world_book", "world_book_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    character_id: Mapped[str] = mapped_column(
        Text, ForeignKey("characters.id", ondelete="RESTRICT"), nullable=False
    )
    world_book_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("world_books.id", ondelete="RESTRICT")
    )
    round_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consolidated_round: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class SessionContextSnapshot(Base):
    """会话创建时冻结的身份、角色卡和可选世界书上下文。"""

    __tablename__ = "session_context_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    identity_source_id: Mapped[str] = mapped_column(Text, nullable=False)
    identity_name: Mapped[str] = mapped_column(Text, nullable=False)
    identity_persona_name: Mapped[str] = mapped_column(Text, nullable=False)
    identity_bio: Mapped[str] = mapped_column(Text, nullable=False)
    character_source_id: Mapped[str] = mapped_column(Text, nullable=False)
    character_name: Mapped[str] = mapped_column(Text, nullable=False)
    character_introduction: Mapped[str] = mapped_column(Text, nullable=False)
    character_system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    character_dialogue_examples_json: Mapped[str] = mapped_column(Text, nullable=False)
    world_book_source_id: Mapped[str | None] = mapped_column(Text)
    world_book_name: Mapped[str | None] = mapped_column(Text)
    world_book_raw_content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)


class SessionWorldBookEntrySnapshot(Base):
    """会话世界书条目的不可变快照。"""

    __tablename__ = "session_world_book_entry_snapshots"
    __table_args__ = (
        CheckConstraint("resident IN (0, 1)", name="ck_session_entry_snapshots_resident"),
        CheckConstraint(
            "index_status IN ('pending', 'ready', 'stale', 'failed')",
            name="ck_session_entry_snapshots_index_status",
        ),
        Index(
            "ux_session_entry_snapshot_source",
            "context_snapshot_id",
            "source_entry_id",
            unique=True,
        ),
        Index(
            "ix_session_entry_snapshot_lookup",
            "context_snapshot_id",
            "resident",
            "position",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    context_snapshot_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("session_context_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_entry_id: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    resident: Mapped[int] = mapped_column(Integer, nullable=False)
    index_status: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_version: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)


class Message(Base):
    """会话中的用户、助手或记忆整理提示消息。"""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'memo')", name="ck_messages_role"),
        CheckConstraint(
            "retrieved_entries_json IS NULL OR role = 'assistant'",
            name="ck_messages_retrieved_only_assistant",
        ),
        Index("ux_messages_position", "session_id", "position", unique=True),
        Index(
            "ux_messages_turn_role",
            "session_id",
            "turn_number",
            "role",
            unique=True,
            sqlite_where=text("turn_number IS NOT NULL"),
        ),
        Index("ix_messages_session_created", "session_id", "created_at", "position"),
        CheckConstraint(
            "turn_number IS NULL OR turn_number > 0",
            name="ck_messages_turn_number",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_number: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_entries_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)


class MessageBlock(Base):
    """保持动作与台词语义及顺序的消息内容块。"""

    __tablename__ = "message_blocks"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="ck_message_blocks_sequence"),
        CheckConstraint("type IN ('action', 'dialogue')", name="ck_message_blocks_type"),
        Index("ux_message_blocks_sequence", "message_id", "sequence", unique=True),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class LongTermMemory(Base):
    """按角色隔离并保留失效历史的长期记忆。"""

    __tablename__ = "long_term_memories"
    __table_args__ = (
        CheckConstraint(
            "type IN ('用户偏好', '角色承诺', '关系变化', '重要剧情', '长期目标')",
            name="ck_long_term_memories_type",
        ),
        CheckConstraint("importance BETWEEN 1 AND 5", name="ck_long_term_memories_importance"),
        CheckConstraint("status IN ('active', 'invalid')", name="ck_long_term_memories_status"),
        CheckConstraint("memory_version >= 1", name="ck_long_term_memories_version"),
        CheckConstraint(
            "index_status IN ('pending', 'ready', 'stale', 'failed')",
            name="ck_long_term_memories_index_status",
        ),
        Index("ix_memories_character_status", "character_id", "status", "type", "created_at"),
        Index("ix_memories_index_status", "index_status", "embedding_version"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(
        Text, ForeignKey("characters.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    index_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    embedding_version: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class MemorySource(Base):
    """长期记忆与来源消息的有序多对多关系。"""

    __tablename__ = "memory_sources"

    memory_id: Mapped[str] = mapped_column(
        Text, ForeignKey("long_term_memories.id", ondelete="CASCADE"), primary_key=True
    )
    message_id: Mapped[str] = mapped_column(
        Text, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    source_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ModelConfig(Base):
    """主模型与 Embedding 模型的非明文配置状态。"""

    __tablename__ = "model_configs"
    __table_args__ = (
        CheckConstraint("group_name IN ('main', 'embed')", name="ck_model_configs_group"),
        CheckConstraint("config_version >= 0", name="ck_model_configs_version"),
        CheckConstraint("rebuild_required IN (0, 1)", name="ck_model_configs_rebuild"),
        CheckConstraint(
            "vector_dimension IS NULL OR vector_dimension > 0",
            name="ck_model_configs_vector_dimension",
        ),
        CheckConstraint(
            "tested_vector_dimension IS NULL OR tested_vector_dimension > 0",
            name="ck_model_configs_tested_vector_dimension",
        ),
    )

    group_name: Mapped[str] = mapped_column(Text, primary_key=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    model_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    secret_ref: Mapped[str | None] = mapped_column(Text)
    key_tail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    vector_dimension: Mapped[int | None] = mapped_column(Integer)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tested_fingerprint: Mapped[str | None] = mapped_column(Text)
    tested_vector_dimension: Mapped[int | None] = mapped_column(Integer)
    tested_at: Mapped[str | None] = mapped_column(Text)
    rebuild_required: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now, onupdate=utc_now)


class BackgroundTask(Base):
    """可恢复的长期记忆与索引后台任务状态。"""

    __tablename__ = "background_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('memory_consolidation', 'index_rebuild', 'vector_cleanup')",
            name="ck_background_tasks_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_background_tasks_status",
        ),
        CheckConstraint("progress_current >= 0", name="ck_background_tasks_current"),
        CheckConstraint("progress_total >= 0", name="ck_background_tasks_total"),
        Index(
            "ux_memory_task_target",
            "task_type",
            "scope_id",
            "target_version",
            unique=True,
            sqlite_where=text("task_type = 'memory_consolidation'"),
        ),
        Index(
            "ux_active_index_rebuild",
            "task_type",
            unique=True,
            sqlite_where=text("task_type = 'index_rebuild' AND status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text)
    target_version: Mapped[int | None] = mapped_column(Integer)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
