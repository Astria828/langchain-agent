"""与 API 契约和前端 TypeScript 类型对齐的 Pydantic DTO。"""

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """统一使用 camelCase 输出，并拒绝未声明字段。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


def _non_blank(value: str) -> str:
    """清理业务名称首尾空白并拒绝空值。"""

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("字段不能为空")
    return cleaned


def _normalized_keywords(values: list[str]) -> list[str]:
    """去除空关键词并按首次出现顺序去重。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = value.strip()
        if keyword and keyword not in seen:
            seen.add(keyword)
            normalized.append(keyword)
    return normalized


class UserIdentity(ApiModel):
    """当前用户身份。"""

    id: str
    name: str
    persona_name: str
    bio: str

    _validate_name = field_validator("name", "persona_name")(_non_blank)


class UpdateUserIdentityPayload(ApiModel):
    """部分更新当前用户身份；至少提交一个契约字段。"""

    name: str | None = None
    persona_name: str | None = None
    bio: str | None = None

    @field_validator("name", "persona_name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        """名称字段存在时去除首尾空白并拒绝空字符串。"""

        return _non_blank(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> "UpdateUserIdentityPayload":
        """拒绝空更新以及显式提交的 null。"""

        if not self.model_fields_set:
            raise ValueError("至少提交一个身份字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("身份字段不能为 null")
        return self


class DialogueExample(ApiModel):
    """一组保持顺序的用户与角色对话示例。"""

    user: str
    assistant: str


class Character(ApiModel):
    """角色卡公开字段。"""

    id: str
    name: str
    introduction: str
    system_prompt: str
    dialogue_examples: list[DialogueExample]

    _validate_name = field_validator("name")(_non_blank)


class UpdateCharacterPayload(ApiModel):
    """部分更新角色卡；字段严格限定为 API 契约中的四项。"""

    name: str | None = None
    introduction: str | None = None
    system_prompt: str | None = None
    dialogue_examples: list[DialogueExample] | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        """角色名称存在时去除首尾空白并拒绝空字符串。"""

        return _non_blank(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> "UpdateCharacterPayload":
        """拒绝空更新以及显式提交的 null。"""

        if not self.model_fields_set:
            raise ValueError("至少提交一个角色卡字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("角色卡字段不能为 null")
        return self


class WorldBookEntry(ApiModel):
    """世界书正式条目。"""

    id: str
    world_book_id: str
    name: str
    content: str
    keywords: list[str]
    category: str
    resident: bool
    enabled: bool
    index_stale: bool = False

    _validate_required_text = field_validator("name", "content")(_non_blank)

    _normalize_keywords = field_validator("keywords")(_normalized_keywords)


class WorldBook(ApiModel):
    """世界书原文及其正式条目。"""

    id: str
    name: str
    raw_content: str
    entries: list[WorldBookEntry]

    _validate_name = field_validator("name")(_non_blank)


class UpdateWorldBookPayload(ApiModel):
    """部分更新世界书名称或完整原文。"""

    name: str | None = None
    raw_content: str | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        """世界书名称存在时去除首尾空白并拒绝空字符串。"""

        return _non_blank(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> "UpdateWorldBookPayload":
        """拒绝空更新以及显式提交的 null。"""

        if not self.model_fields_set:
            raise ValueError("至少提交一个世界书字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("世界书字段不能为 null")
        return self


class WorldBookDraftEntry(ApiModel):
    """尚未写入正式表的世界书拆分草稿。"""

    name: str
    category: str
    content: str

    _validate_required_text = field_validator("name", "content")(_non_blank)


class ConfirmWorldBookDraftsPayload(ApiModel):
    """用户确认后可写入正式条目的世界书草稿。"""

    drafts: Annotated[list[WorldBookDraftEntry], Field(min_length=1)]


class WorldBookSplitOutput(ApiModel):
    """主模型拆分世界书时必须返回的结构化结果。"""

    drafts: Annotated[list[WorldBookDraftEntry], Field(min_length=1)]


class UpdateWorldBookEntryPayload(ApiModel):
    """部分更新世界书条目的六个可编辑字段。"""

    name: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    category: str | None = None
    resident: bool | None = None
    enabled: bool | None = None

    @field_validator("name", "content")
    @classmethod
    def validate_optional_required_text(cls, value: str | None) -> str | None:
        """名称和正文存在时去除首尾空白并拒绝空字符串。"""

        return _non_blank(value) if value is not None else None

    @field_validator("keywords")
    @classmethod
    def normalize_optional_keywords(cls, values: list[str] | None) -> list[str] | None:
        """关键词存在时执行与响应 DTO 相同的稳定清理。"""

        return _normalized_keywords(values) if values is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> "UpdateWorldBookEntryPayload":
        """拒绝空更新以及显式提交的 null。"""

        if not self.model_fields_set:
            raise ValueError("至少提交一个世界书条目字段")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("世界书条目字段不能为 null")
        return self


class ReembedResult(ApiModel):
    """单本世界书重新生成向量的成功数量。"""

    count: Annotated[int, Field(ge=0)]


class DeleteLogsResult(ApiModel):
    """日志删除操作实际移除的记录数量。"""

    count: Annotated[int, Field(ge=0)]


class Session(ApiModel):
    """会话公开字段。"""

    id: str
    title: str
    character_id: str
    world_book_id: str | None
    identity_snapshot_id: str
    identity_name: str
    identity_persona_name: str
    character_name: str
    world_book_name: str | None
    round_count: Annotated[int, Field(ge=0)]
    consolidated_round: Annotated[int, Field(ge=0)]
    summary: str
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_rounds(self) -> "Session":
        """已整理轮次不能超过已完成轮次。"""

        if self.consolidated_round > self.round_count:
            raise ValueError("consolidatedRound 不能超过 roundCount")
        return self


class CreateSessionPayload(ApiModel):
    """创建会话所需的固定绑定。"""

    character_id: str
    world_book_id: str | None


class UpdateSessionPayload(ApiModel):
    """会话创建后只允许修改标题。"""

    title: str

    _validate_title = field_validator("title")(_non_blank)


class SendMessagePayload(ApiModel):
    """发送给当前会话的单条用户输入。"""

    content: str

    _validate_content = field_validator("content")(_non_blank)


BlockType = Literal["action", "dialogue"]
MessageRole = Literal["user", "assistant", "memo"]


class ChatReplyBlock(ApiModel):
    """主模型返回的单个角色回复内容块。"""

    type: BlockType
    content: str

    _validate_content = field_validator("content")(_non_blank)


class ChatReplyOutput(ApiModel):
    """主模型必须遵循的有序角色回复结构。"""

    blocks: Annotated[list[ChatReplyBlock], Field(min_length=1)]


class MessageBlock(ApiModel):
    """动作或台词消息块。"""

    id: str
    sequence: Annotated[int, Field(ge=0)]
    type: BlockType
    content: str


class Message(ApiModel):
    """按内容块表达的一条会话消息。"""

    id: str
    session_id: str
    role: MessageRole
    blocks: list[MessageBlock]
    created_at: str
    retrieved: list[str] = Field(default_factory=list)


class RecommendedReplyOutput(ApiModel):
    """根据当前会话生成、但尚未发送的用户侧推荐回复。"""

    content: str

    _validate_content = field_validator("content")(_non_blank)


MemoryType = Literal["用户偏好", "角色承诺", "关系变化", "重要剧情", "长期目标"]
MemoryStatus = Literal["有效", "已失效"]
MemoryConsolidationAction = Literal[
    "create",
    "deduplicate",
    "merge",
    "update",
    "invalidate",
    "ignore",
]


class MemoryCandidate(ApiModel):
    """从一批十轮对话中提取的单条长期记忆候选。"""

    type: MemoryType
    content: str
    importance: Annotated[int, Field(ge=1, le=5)]
    source_rounds: Annotated[
        list[Annotated[int, Field(ge=1, le=10)]],
        Field(min_length=1),
    ]

    _validate_content = field_validator("content")(_non_blank)

    @field_validator("source_rounds")
    @classmethod
    def validate_source_rounds(cls, value: list[int]) -> list[int]:
        """来源轮次必须唯一，避免后续重复写入来源关系。"""

        if len(value) != len(set(value)):
            raise ValueError("来源轮次不能重复")
        return value


class MemoryExtractionOutput(ApiModel):
    """记忆提取 Chain 的结构化输出，允许本批次没有候选。"""

    candidates: list[MemoryCandidate]


class MemoryConsolidationReference(ApiModel):
    """提供给整合 Chain 的同角色已有记忆最小视图。"""

    id: str
    type: MemoryType
    content: str
    importance: Annotated[int, Field(ge=1, le=5)]

    _validate_required_text = field_validator("id", "content")(_non_blank)


class MemoryConsolidationInput(ApiModel):
    """单条候选及其有限的同角色相似记忆。"""

    candidate: MemoryCandidate
    existing_memories: list[MemoryConsolidationReference]


class MemoryConsolidationDecision(ApiModel):
    """整合 Chain 对单条候选给出的唯一动作。"""

    action: MemoryConsolidationAction
    target_memory_id: str | None = None
    content: str | None = None
    importance: Annotated[int, Field(ge=1, le=5)] | None = None

    @field_validator("target_memory_id", "content")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        """可选文本一旦出现就必须是非空值。"""

        return _non_blank(value) if value is not None else None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "MemoryConsolidationDecision":
        """按动作约束目标 ID 和最终记忆正文，拒绝含糊决策。"""

        target_actions = {"deduplicate", "merge", "update", "invalidate"}
        result_actions = {"create", "merge", "update"}

        if self.action in target_actions and self.target_memory_id is None:
            raise ValueError("当前整合动作必须提供目标记忆 ID")
        if self.action not in target_actions and self.target_memory_id is not None:
            raise ValueError("当前整合动作不能提供目标记忆 ID")
        if self.action in result_actions and (self.content is None or self.importance is None):
            raise ValueError("当前整合动作必须提供最终记忆正文和重要性")
        if self.action not in result_actions and (
            self.content is not None or self.importance is not None
        ):
            raise ValueError("当前整合动作不能提供最终记忆正文或重要性")
        return self


class LongTermMemory(ApiModel):
    """前端展示的长期记忆。"""

    id: str
    character_id: str
    type: MemoryType
    content: str
    importance: Annotated[int, Field(ge=1, le=5)]
    status: MemoryStatus
    created_at: str
    source_label: str


ModelGroup = Literal["main", "embed"]


class ModelEndpointConfig(ApiModel):
    """不包含明文密钥的模型端点配置。"""

    base_url: str
    model: str
    key_set: bool
    key_tail: str


class ModelEndpointPayload(ApiModel):
    """仅在测试或保存时接收的模型端点参数。"""

    base_url: str
    model: str
    api_key: str

    _validate_required_text = field_validator("base_url", "model")(_non_blank)


class ModelSettings(ApiModel):
    """主模型与 Embedding 模型配置集合。"""

    main: ModelEndpointConfig
    embed: ModelEndpointConfig


class ConnectionTestResult(ApiModel):
    """模型连接测试结果。"""

    ok: bool
    message: str


class IndexStatus(ApiModel):
    """全局向量索引重建状态。"""

    rebuild_required: bool


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogRange = Literal["all", "1d", "7d", "30d"]


class LogEntry(ApiModel):
    """日志页面使用的脱敏条目。"""

    id: str
    date: str
    time: str
    level: LogLevel
    module: str
    event: str
    request_id: str
    message: str
    business_ids: dict[str, str] = Field(default_factory=dict)


ResponseData = TypeVar("ResponseData")


class DataResponse(ApiModel, Generic[ResponseData]):
    """普通 JSON 接口统一成功响应壳。"""

    data: ResponseData
    request_id: str
