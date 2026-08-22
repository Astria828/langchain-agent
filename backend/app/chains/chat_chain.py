"""按固定应用规则生成结构化角色回复的对话 Chain。"""

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import ValidationError

from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway, ModelStreamChunk
from app.schemas.dto import ChatReplyBlock, ChatReplyOutput, RecommendedReplyOutput

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "chat.md"
RECOMMENDED_REPLY_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "recommended_reply.md"
)
BLOCKS_ARRAY_PATTERN = re.compile(r'"blocks"\s*:\s*\[')


async def stream_basic_chat_blocks(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict[str, str]],
) -> AsyncIterator[ChatReplyBlock | ModelStreamChunk]:
    """流式读取结构化回复，转发推理增量，只在块完整校验后交付正文。"""

    parser = PydanticOutputParser(pydantic_object=ChatReplyOutput)
    application_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{parser.get_format_instructions()}"
    )
    raw_parts: list[str] = []
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
    ):
        # 推理增量不参与 JSON 结构解析，只用于向上层汇报生成状态。
        if chunk.kind == "reasoning":
            yield chunk
            continue

        raw_parts.append(chunk.text)
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

    raw_response = "".join(raw_parts)
    if not array_started or not array_closed or object_depth != 0 or not emitted_blocks:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块结构不完整",
        )
    try:
        output = parser.parse(raw_response)
    except OutputParserException as exc:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块结构无效",
        ) from exc
    if output.blocks != emitted_blocks:
        raise AppError(
            status_code=502,
            code="MODEL_OUTPUT_INVALID",
            message="主模型返回的内容块前后不一致",
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
            ],
        )
        return parser.parse(raw_response)

    return RunnableLambda(generate)
