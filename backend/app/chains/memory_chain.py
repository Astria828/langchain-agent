"""从十轮对话提取长期记忆，并与同角色已有记忆进行结构化整合。"""

from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda

from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway
from app.schemas.dto import (
    MemoryConsolidationDecision,
    MemoryConsolidationInput,
    MemoryExtractionOutput,
)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "memory.md"


def build_memory_extraction_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
) -> Runnable[str, MemoryExtractionOutput]:
    """构造五类长期记忆提取 Runnable，格式异常时整批失败。"""

    parser = PydanticOutputParser(pydantic_object=MemoryExtractionOutput)
    system_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n"
        "当前任务：长期记忆提取。只执行下文的提取任务规则。\n\n"
        f"{parser.get_format_instructions()}"
    )

    async def extract(conversation: str) -> MemoryExtractionOutput:
        """调用主模型并严格校验候选类型、重要性和来源轮次。"""

        raw_response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": conversation},
            ],
        )
        try:
            return parser.parse(raw_response)
        except OutputParserException as exc:
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="长期记忆提取结果格式无效",
            ) from exc

    return RunnableLambda(extract)


def build_memory_consolidation_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
) -> Runnable[MemoryConsolidationInput, MemoryConsolidationDecision]:
    """构造单候选记忆整合 Runnable，并限制目标为本次提供的已有记忆。"""

    parser = PydanticOutputParser(pydantic_object=MemoryConsolidationDecision)
    system_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n"
        "当前任务：长期记忆整合。只执行下文的整合任务规则。\n\n"
        f"{parser.get_format_instructions()}"
    )

    async def consolidate(
        consolidation_input: MemoryConsolidationInput,
    ) -> MemoryConsolidationDecision:
        """调用主模型，并拒绝引用输入集合之外的记忆 ID。"""

        raw_response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": consolidation_input.model_dump_json(by_alias=True),
                },
            ],
        )
        try:
            decision = parser.parse(raw_response)
        except OutputParserException as exc:
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="长期记忆整合结果格式无效",
            ) from exc

        existing_ids = {memory.id for memory in consolidation_input.existing_memories}
        if decision.target_memory_id is not None and decision.target_memory_id not in existing_ids:
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="长期记忆整合结果引用了无效目标",
            )
        return decision

    return RunnableLambda(consolidate)
