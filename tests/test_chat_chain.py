"""阶段 5 基础角色对话 Chain 测试。"""

import asyncio

from app.chains.chat_chain import build_basic_chat_chain


class StubGateway:
    """返回固定主模型文本并记录最终消息顺序。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict[str, str]] = []

    async def create_chat_completion(self, **kwargs) -> str:
        """记录调用并返回测试文本。"""

        self.messages = kwargs["messages"]
        return self.response


def test_basic_chat_chain_parses_ordered_action_and_dialogue_blocks() -> None:
    """有效结构化回复保持模型给出的内容块顺序。"""

    gateway = StubGateway(
        '{"blocks":['
        '{"type":"action","content":"她抬起头。"},'
        '{"type":"dialogue","content":"你来了。"}'
        "]}"
    )
    chain = build_basic_chat_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    output = asyncio.run(chain.ainvoke([{"role": "user", "content": "我推开门。"}]))

    assert [block.type for block in output.blocks] == ["action", "dialogue"]
    assert output.blocks[1].content == "你来了。"
    assert gateway.messages[0]["role"] == "system"
    assert "仅输出指定的结构化数据" in gateway.messages[0]["content"]
    assert gateway.messages[-1] == {"role": "user", "content": "我推开门。"}


def test_basic_chat_chain_preserves_invalid_model_output_as_dialogue() -> None:
    """模型没有遵守结构时保留完整原文，不丢弃角色回复。"""

    gateway = StubGateway("她看向门口。\n你终于来了。")
    chain = build_basic_chat_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    output = asyncio.run(chain.ainvoke([{"role": "user", "content": "你好"}]))

    assert len(output.blocks) == 1
    assert output.blocks[0].type == "dialogue"
    assert output.blocks[0].content == "她看向门口。\n你终于来了。"


def test_basic_chat_chain_preserves_schema_invalid_json_as_dialogue() -> None:
    """JSON 外形正确但内容块为空时也保留完整原文，覆盖 Parser 校验失败分支。"""

    raw_response = '{"blocks":[{"type":"dialogue","content":"   "}]}'
    gateway = StubGateway(raw_response)
    chain = build_basic_chat_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    output = asyncio.run(chain.ainvoke([{"role": "user", "content": "继续"}]))

    assert len(output.blocks) == 1
    assert output.blocks[0].type == "dialogue"
    assert output.blocks[0].content == raw_response
