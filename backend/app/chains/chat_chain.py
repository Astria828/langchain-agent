"""按固定应用规则生成结构化角色回复的对话 Chain。"""

from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable, RunnableLambda

from app.gateways.model_gateway import ModelGateway
from app.schemas.dto import ChatReplyBlock, ChatReplyOutput

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "chat.md"


def build_basic_chat_chain(
    *,
    gateway: ModelGateway,
    base_url: str,
    model: str,
    api_key: str,
) -> Runnable[list[dict[str, str]], ChatReplyOutput]:
    """构造固定应用 Prompt、结构校验和保留原文降级的对话 Runnable。"""

    parser = PydanticOutputParser(pydantic_object=ChatReplyOutput)
    application_prompt = (
        f"{PROMPT_PATH.read_text(encoding='utf-8').strip()}\n\n{parser.get_format_instructions()}"
    )

    async def generate(messages: list[dict[str, str]]) -> ChatReplyOutput:
        """调用主模型；协议异常时把完整原文降级为单个 dialogue 块。"""

        raw_response = await gateway.create_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": application_prompt},
                *messages,
            ],
        )
        try:
            return parser.parse(raw_response)
        except OutputParserException:
            return ChatReplyOutput(blocks=[ChatReplyBlock(type="dialogue", content=raw_response)])

    return RunnableLambda(generate)
