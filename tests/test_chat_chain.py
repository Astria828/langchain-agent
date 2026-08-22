"""基础角色对话的结构化内容块流测试。"""

import asyncio

import pytest
from app.chains.chat_chain import stream_basic_chat_blocks
from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelStreamChunk


class StubGateway:
    """按指定网络分片返回主模型文本并记录最终消息顺序。"""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.messages: list[dict[str, str]] = []

    async def stream_chat_completion(self, **kwargs):
        """记录调用并按顺序产生测试分片。"""

        self.messages = kwargs["messages"]
        for chunk in self.chunks:
            yield chunk if isinstance(chunk, ModelStreamChunk) else ModelStreamChunk("content", chunk)


def collect_blocks(gateway: StubGateway) -> list:
    """收集一次基础角色回复产生的完整内容块。"""

    async def collect() -> list:
        return [
            block
            async for block in stream_basic_chat_blocks(
                gateway=gateway,
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="secret",
                messages=[{"role": "user", "content": "我推开门。"}],
            )
        ]

    return asyncio.run(collect())


def test_basic_chat_stream_parses_ordered_blocks_across_network_chunks() -> None:
    """字段名、中文正文和对象边界被任意拆分时仍按完整块交付。"""

    gateway = StubGateway(
        [
            '{"blo',
            'cks":[{"type":"action","content":"她抬',
            '起头。"},',
            '{"type":"dialogue","content":"你来了。"}',
            "]}",
        ]
    )

    blocks = collect_blocks(gateway)

    assert [block.type for block in blocks] == ["action", "dialogue"]
    assert [block.content for block in blocks] == ["她抬起头。", "你来了。"]
    assert gateway.messages[0]["role"] == "system"
    assert "仅输出指定的结构化数据" in gateway.messages[0]["content"]
    assert gateway.messages[-1] == {"role": "user", "content": "我推开门。"}


def test_basic_chat_stream_ignores_json_symbols_inside_escaped_content() -> None:
    """正文内的引号、反斜杠和花括号不能被误判为内容块边界。"""

    raw = (
        '{"blocks":['
        '{"type":"dialogue","content":"她说：\\"看这里 {别动}\\\\路径\\"。"},'
        '{"type":"action","content":"她合上书。"}'
        "]}"
    )
    blocks = collect_blocks(StubGateway([raw[:31], raw[31:57], raw[57:]]))

    assert len(blocks) == 2
    assert blocks[0].content == '她说："看这里 {别动}\\路径"。'
    assert blocks[1].type == "action"


@pytest.mark.parametrize(
    "raw_response",
    [
        "她看向门口。\n你终于来了。",
        '{"blocks":[{"type":"dialogue","content":"   "}]}',
        '{"blocks":[{"type":"unknown","content":"内容"}]}',
        '{"blocks":[{"type":"dialogue","content":"未闭合"}',
    ],
)
def test_basic_chat_stream_rejects_unstructured_or_incomplete_output(raw_response: str) -> None:
    """流式对话不把协议错误静默降级为台词，避免展示与落库不一致。"""

    with pytest.raises(AppError) as error:
        collect_blocks(StubGateway([raw_response]))

    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_stream_blocks_discards_reasoning_without_breaking_json_parsing() -> None:
    """推理增量被丢弃，穿插在正文分片之间也不影响内容块解析。"""

    gateway = StubGateway(
        [
            ModelStreamChunk("reasoning", "在想怎么回应"),
            ModelStreamChunk("content", '{"blocks":[{"type":"action",'),
            ModelStreamChunk("reasoning", "补一句思考"),
            ModelStreamChunk("content", '"content":"她抬起头。"}]}'),
        ]
    )

    blocks = collect_blocks(gateway)

    assert [(block.type, block.content) for block in blocks] == [
        ("action", "她抬起头。")
    ]
