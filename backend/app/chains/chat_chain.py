"""按固定应用规则生成结构化角色回复的对话 Chain。"""

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway
from app.schemas.dto import BlockType, ChatReplyBlock, ChatReplyOutput, RecommendedReplyOutput

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "chat.md"
RECOMMENDED_REPLY_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "recommended_reply.md"
)
BLOCKS_ARRAY_PATTERN = re.compile(r'"blocks"\s*:\s*\[')

# 兜底解析识别的行首标记。`[动作]/[台词]` 是历史消息回喂给模型时用过的写法，
# 上游没有执行结构化约束时模型最容易照抄成这一种；其余拼写只是顺带容错。
BLOCK_MARKER_PATTERN = re.compile(
    r"^\s*[\[【]\s*(动作|台词|对话|action|dialogue)\s*[\]】]\s*[:：]?\s*(.*)$",
    re.IGNORECASE,
)
MARKER_BLOCK_TYPES: dict[str, BlockType] = {
    "动作": "action",
    "action": "action",
    "台词": "dialogue",
    "对话": "dialogue",
    "dialogue": "dialogue",
}

# 发给上游的 JSON Schema 手写而不是从 ChatReplyOutput 生成：
# strict 模式要求每层都写死 additionalProperties=false 且不接受 minItems 这类关键字，
# 而 Pydantic 侧的校验规则可以独立演进。两者的字段契约必须保持一致。
REPLY_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["blocks"],
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "content"],
                "properties": {
                    "type": {"type": "string", "enum": ["action", "dialogue"]},
                    "content": {"type": "string"},
                },
            },
        }
    },
}
REPLY_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "chat_reply",
        "strict": True,
        "schema": REPLY_JSON_SCHEMA,
    },
}

RECOMMENDED_REPLY_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content"],
    "properties": {"content": {"type": "string"}},
}
RECOMMENDED_REPLY_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "recommended_reply",
        "strict": True,
        "schema": RECOMMENDED_REPLY_JSON_SCHEMA,
    },
}

# 推荐回复的历史以角色的回复收尾，若直接请求生成，模型会把它当成
# “继续补完角色刚才那段话”，返回 " C" 这类续写片段而不是结构化数据。
# 末尾补一轮用户消息把发言权交回来，模型才会按给定的 schema 作答。
RECOMMENDED_REPLY_DIRECTIVE = (
    "（系统指令，不是对话内容）请按上面给定的 JSON 结构，"
    "只输出我接下来可以直接发送的一句话。"
)


def _blocks_from_json(raw: str) -> list[ChatReplyBlock]:
    """从整段原文的 blocks 数组里逐个取出内容块对象。

    与流式扫描同一套字符状态机，区别只在这里跳过读不懂的部分而不是整条判废：
    数组里夹杂的解说文字、尾随逗号、被截断的末尾都不影响已经完整的对象。
    """

    match = BLOCKS_ARRAY_PATTERN.search(raw)
    if match is None:
        return []

    blocks: list[ChatReplyBlock] = []
    object_chars: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    for char in raw[match.end() :]:
        if depth == 0:
            if char == "]":
                break
            if char != "{":
                continue
            object_chars = [char]
            depth = 1
            in_string = False
            escaped = False
            continue

        object_chars.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                with suppress(json.JSONDecodeError, ValidationError):
                    block_data = json.loads("".join(object_chars))
                    blocks.append(ChatReplyBlock.model_validate(block_data))
                object_chars = []
    return blocks


def _blocks_from_markers(raw: str) -> list[ChatReplyBlock]:
    """把 `[动作]/[台词]` 逐行文本还原成内容块，未带标记的续行并入上一块。"""

    drafts: list[list[str]] = []
    for line in raw.splitlines():
        match = BLOCK_MARKER_PATTERN.match(line)
        if match is not None:
            drafts.append([MARKER_BLOCK_TYPES[match.group(1).lower()], match.group(2).strip()])
            continue
        text = line.strip()
        if not text or not drafts:
            continue
        drafts[-1][1] = f"{drafts[-1][1]}\n{text}".strip()

    return [
        ChatReplyBlock(type=block_type, content=content)
        for block_type, content in drafts
        if content
    ]


def salvage_reply_blocks(raw: str) -> list[ChatReplyBlock]:
    """在严格流式解析失败后，尽力从整段原文里还原内容块。

    上游不是每次都会真正执行 json_schema：OpenRouter 这类网关会把同一个模型
    轮流路由到十几家服务商，其中少数几家收下 response_format、返回 HTTP 200、
    然后完全无视它。这种静默丢弃既不触发网关的降级重试，也不产生任何告警，
    模型于是按提示词或历史示范的写法自由输出。这些写法携带的类型与顺序信息
    与内容块完全等价，能还原就不该把整条回复判废。
    """

    return _blocks_from_json(raw) or _blocks_from_markers(raw)


async def stream_basic_chat_blocks(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
    extra_body: dict[str, object] | None = None,
) -> AsyncIterator[ChatReplyBlock]:
    """流式读取结构化回复，只在单个动作或台词块完整校验后交付。"""

    parser = PydanticOutputParser(pydantic_object=ChatReplyOutput)
    application_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{parser.get_format_instructions()}"
    )
    prefix = ""
    raw_parts: list[str] = []
    object_chars: list[str] = []
    emitted_blocks: list[ChatReplyBlock] = []
    array_started = False
    array_closed = False
    strict_failed = False
    object_depth = 0
    in_string = False
    escaped = False

    async for chunk in gateway.stream_chat_completion(
        base_url=base_url,
        model=model,
        api_key=api_key,
        messages=[
            {"role": "system", "content": application_prompt},
            *messages,
        ],
        # 不显式给上限时按上游默认值截断，长回复会在 JSON 中途被切断。
        max_tokens=get_settings().chat_max_tokens,
        response_format=REPLY_RESPONSE_FORMAT,
        extra_body=extra_body,
    ):
        # 推理增量不是回复正文，直接丢弃，避免污染 JSON 结构解析。
        if chunk.kind == "reasoning":
            continue

        raw_parts.append(chunk.text)
        # 严格解析已经放弃，但整段原文还要读完，兜底解析要在结束后用它重来一遍。
        if strict_failed:
            continue

        pending = chunk.text
        if not array_started:
            prefix += pending
            match = BLOCKS_ARRAY_PATTERN.search(prefix)
            if match is None:
                continue
            array_started = True
            pending = prefix[match.end() :]
            prefix = ""

        # 从 blocks 数组起逐字符维护 JSON 字符串与花括号状态，避免正文中的符号误判边界。
        for char in pending:
            if array_closed:
                continue
            if object_depth == 0:
                if char.isspace() or char == ",":
                    continue
                if char == "]":
                    array_closed = True
                    continue
                if char != "{":
                    strict_failed = True
                    break
                object_chars = [char]
                object_depth = 1
                in_string = False
                escaped = False
                continue

            object_chars.append(char)
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                object_depth += 1
            elif char == "}":
                object_depth -= 1
                if object_depth == 0:
                    try:
                        block_data = json.loads("".join(object_chars))
                        block = ChatReplyBlock.model_validate(block_data)
                    except (json.JSONDecodeError, ValidationError):
                        strict_failed = True
                        break
                    emitted_blocks.append(block)
                    yield block
                    object_chars = []

    # 逐块解析时已经用 ChatReplyBlock 校验过每个内容块，这里只确认数组本身收尾完整。
    # 不再对整段原文做二次解析：那会让 JSON 前后的寒暄或尾随逗号
    # 把已经正确交付的内容块整条判废。
    strict_complete = (
        not strict_failed
        and array_started
        and array_closed
        and object_depth == 0
        and bool(emitted_blocks)
    )
    if strict_complete:
        return

    # 已经交付出去的内容块撤不回来，再按兜底结果重拼一遍只会和前端已展示的内容重复。
    # 这种半条回复按失败处理，与“格式异常不保存部分助手回复”的约定一致。
    if emitted_blocks:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message=(
                "主模型返回的内容块结构无效"
                if strict_failed
                else "主模型返回的内容块结构不完整"
            ),
        )

    salvaged = salvage_reply_blocks("".join(raw_parts))
    if not salvaged:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块结构无效",
        )
    logger.warning(
        "主模型未按结构化约束输出，已从原文还原内容块 model=%s blocks=%s",
        model,
        len(salvaged),
        extra={
            "event": "chat_reply_structure_salvaged",
            "business_ids": {"model": model, "blockCount": str(len(salvaged))},
        },
    )
    for block in salvaged:
        yield block


def build_recommended_reply_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
    extra_body: dict[str, object] | None = None,
) -> Runnable[list[dict[str, str]], RecommendedReplyOutput]:
    """根据当前会话上下文和最近历史生成一条尚未发送的用户回复。"""

    parser = PydanticOutputParser(pydantic_object=RecommendedReplyOutput)
    # 规则跟在历史之后而不是放在最前面的 system：角色卡本身就是一整套扮演指令，
    # 会话越长，开头那份规则离生成点越远，模型越容易接着角色的语气继续往下写。
    application_prompt = (
        f"{RECOMMENDED_REPLY_PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n"
        f"{parser.get_format_instructions()}\n\n"
        f"{RECOMMENDED_REPLY_DIRECTIVE}"
    )

    async def generate(messages: list[dict[str, str]]) -> RecommendedReplyOutput:
        """调用主模型并严格解析推荐回复结构。"""

        raw_response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                *messages,
                {"role": "user", "content": application_prompt},
            ],
            response_format=RECOMMENDED_REPLY_RESPONSE_FORMAT,
            extra_body=extra_body,
        )
        return parser.parse(raw_response)

    return RunnableLambda(generate)
