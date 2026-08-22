"""按固定应用规则生成结构化角色回复的对话 Chain。"""

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway
from app.schemas.dto import ChatReplyBlock, ChatReplyOutput, RecommendedReplyOutput

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "chat.md"
RECOMMENDED_REPLY_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "recommended_reply.md"
)
BLOCKS_ARRAY_PATTERN = re.compile(r'"blocks"\s*:\s*\[')

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

# 推荐回复的历史以角色的回复收尾，若直接请求生成，模型会把它当成
# “继续补完角色刚才那段话”，返回 " C" 这类续写片段而不是结构化数据。
# 末尾补一轮用户消息把发言权交回来，模型才会按 system 里的 schema 作答。
RECOMMENDED_REPLY_DIRECTIVE = (
    "（系统指令，不是对话内容）请按上面给定的 JSON 结构，"
    "只输出我接下来可以直接发送的一句话。"
)


async def stream_basic_chat_blocks(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
) -> AsyncIterator[ChatReplyBlock]:
    """流式读取结构化回复，只在单个动作或台词块完整校验后交付。"""

    parser = PydanticOutputParser(pydantic_object=ChatReplyOutput)
    application_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{parser.get_format_instructions()}"
    )
    prefix = ""
    object_chars: list[str] = []
    emitted_blocks: list[ChatReplyBlock] = []
    array_started = False
    array_closed = False
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
    ):
        # 推理增量不是回复正文，直接丢弃，避免污染 JSON 结构解析。
        if chunk.kind == "reasoning":
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
                    raise AppError(
                        status_code=502,
                        code="MODEL_OUTPUT_INVALID",
                        message="主模型返回的内容块结构无效",
                    )
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
                    except (json.JSONDecodeError, ValidationError) as exc:
                        raise AppError(
                            status_code=502,
                            code="MODEL_OUTPUT_INVALID",
                            message="主模型返回的内容块结构无效",
                        ) from exc
                    emitted_blocks.append(block)
                    yield block
                    object_chars = []

    # 逐块解析时已经用 ChatReplyBlock 校验过每个内容块，这里只确认数组本身收尾完整。
    # 不再对整段原文做二次解析：那会让 JSON 前后的寒暄或尾随逗号
    # 把已经正确交付的内容块整条判废。
    if not array_started or not array_closed or object_depth != 0 or not emitted_blocks:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块结构不完整",
        )


def build_recommended_reply_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
) -> Runnable[list[dict[str, str]], RecommendedReplyOutput]:
    """根据当前会话上下文和最近历史生成一条尚未发送的用户回复。"""

    parser = PydanticOutputParser(pydantic_object=RecommendedReplyOutput)
    application_prompt = (
        f"{RECOMMENDED_REPLY_PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n"
        f"{parser.get_format_instructions()}"
    )

    async def generate(messages: list[dict[str, str]]) -> RecommendedReplyOutput:
        """调用主模型并严格解析推荐回复结构。"""

        raw_response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": application_prompt},
                *messages,
                {"role": "user", "content": RECOMMENDED_REPLY_DIRECTIVE},
            ],
        )
        return parser.parse(raw_response)

    return RunnableLambda(generate)
