"""阶段 4 世界书拆分 Chain 单元测试。"""

import asyncio

import pytest
from app.chains.worldbook_split_chain import build_worldbook_split_chain
from app.core.exceptions import AppError


class StubGateway:
    """记录 Chain 请求并返回固定主模型文本。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict[str, str]] = []

    async def create_chat_completion(self, **kwargs) -> str:
        """保存消息，避免测试访问真实模型服务。"""

        self.messages = kwargs["messages"]
        return self.response


def test_split_chain_returns_validated_drafts_without_rewriting_source() -> None:
    """Chain 将原文作为用户数据传递，并校验结构化草稿。"""

    gateway = StubGateway(
        '{"drafts":[{"name":"银港","category":"地点","content":"银港终年被海雾笼罩。"}]}'
    )
    chain = build_worldbook_split_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="sk-test-main",
    )

    drafts = asyncio.run(chain.ainvoke("银港终年被海雾笼罩。"))

    assert drafts[0].name == "银港"
    assert drafts[0].content == "银港终年被海雾笼罩。"
    assert "不得扩写" in gateway.messages[0]["content"]
    assert gateway.messages[1] == {"role": "user", "content": "银港终年被海雾笼罩。"}


def test_split_chain_rejects_invalid_model_output() -> None:
    """无法解析的模型输出不会被当作草稿继续使用。"""

    chain = build_worldbook_split_chain(
        gateway=StubGateway("这不是结构化数据"),
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="sk-test-main",
    )

    with pytest.raises(AppError) as error:
        asyncio.run(chain.ainvoke("原文"))

    assert error.value.status_code == 502
    assert error.value.code == "MODEL_OUTPUT_INVALID"
