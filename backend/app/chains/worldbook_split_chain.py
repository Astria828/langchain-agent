"""将世界书原文拆分为待用户确认的结构化草稿。"""

from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda

from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway
from app.schemas.dto import WorldBookDraftEntry, WorldBookSplitOutput

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "worldbook_split.md"


def build_worldbook_split_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
) -> Runnable[str, list[WorldBookDraftEntry]]:
    """构造只负责世界书拆分和结构化校验的异步 Runnable。"""

    parser = PydanticOutputParser(pydantic_object=WorldBookSplitOutput)
    system_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{parser.get_format_instructions()}"
    )

    async def split(raw_content: str) -> list[WorldBookDraftEntry]:
        """调用主模型，并拒绝任何不符合草稿 DTO 的输出。"""

        response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_content},
            ],
        )
        try:
            return parser.parse(response).drafts
        except OutputParserException as exc:
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="世界书拆分结果格式无效",
            ) from exc

    return RunnableLambda(split)
