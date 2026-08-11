"""阶段 7.1 长期记忆提取与整合 Chain 测试。"""

import asyncio

import pytest
from app.chains.memory_chain import (
    build_memory_consolidation_chain,
    build_memory_extraction_chain,
)
from app.core.exceptions import AppError
from app.schemas.dto import MemoryConsolidationInput


class StubGateway:
    """记录 Chain 请求并返回固定主模型文本。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict[str, str]] = []

    async def create_chat_completion(self, **kwargs) -> str:
        """保存最终消息，避免测试访问真实模型服务。"""

        self.messages = kwargs["messages"]
        return self.response


def build_consolidation_input() -> MemoryConsolidationInput:
    """创建包含单条候选和一个合法目标的整合输入。"""

    return MemoryConsolidationInput.model_validate(
        {
            "candidate": {
                "type": "角色承诺",
                "content": "艾拉承诺陪用户寻找旧档案馆。",
                "importance": 4,
                "sourceRounds": [3, 7],
            },
            "existingMemories": [
                {
                    "id": "memory-1",
                    "type": "角色承诺",
                    "content": "艾拉承诺帮助用户寻找档案馆。",
                    "importance": 3,
                }
            ],
        }
    )


def test_memory_extraction_chain_parses_five_types_and_sources() -> None:
    """提取 Chain 保留五类枚举、重要性和对应来源轮次。"""

    gateway = StubGateway(
        '{"candidates":['
        '{"type":"用户偏好","content":"用户喜欢雨天。","importance":2,"sourceRounds":[1]},'
        '{"type":"角色承诺","content":"艾拉承诺同行。","importance":4,"sourceRounds":[2]},'
        '{"type":"关系变化","content":"双方开始互相信任。","importance":4,"sourceRounds":[3]},'
        '{"type":"重要剧情","content":"找到了旧档案馆。","importance":5,"sourceRounds":[4]},'
        '{"type":"长期目标","content":"共同修复档案馆。","importance":5,"sourceRounds":[5,8]}'
        "]}"
    )
    chain = build_memory_extraction_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    output = asyncio.run(chain.ainvoke("第 1 至 10 轮完整对话"))

    assert [candidate.type for candidate in output.candidates] == [
        "用户偏好",
        "角色承诺",
        "关系变化",
        "重要剧情",
        "长期目标",
    ]
    assert output.candidates[-1].source_rounds == [5, 8]
    assert "不保存普通寒暄" in gateway.messages[0]["content"]
    assert gateway.messages[1] == {"role": "user", "content": "第 1 至 10 轮完整对话"}


def test_memory_extraction_chain_accepts_empty_candidates() -> None:
    """没有长期价值的批次可以返回空候选，而不是制造记忆。"""

    chain = build_memory_extraction_chain(
        gateway=StubGateway('{"candidates":[]}'),
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    assert asyncio.run(chain.ainvoke("十轮普通寒暄")).candidates == []


@pytest.mark.parametrize(
    "response",
    [
        '{"candidates":[{"type":"临时事件","content":"内容","importance":3,"sourceRounds":[1]}]}',
        '{"candidates":[{"type":"重要剧情","content":"内容","importance":6,"sourceRounds":[1]}]}',
        '{"candidates":[{"type":"重要剧情","content":"内容","importance":3,"sourceRounds":[11]}]}',
        '{"candidates":[{"type":"重要剧情","content":"内容","importance":3,"sourceRounds":[1,1]}]}',
    ],
)
def test_memory_extraction_chain_rejects_invalid_candidates(response: str) -> None:
    """非法类型、重要性和来源轮次不能进入后续写库流程。"""

    chain = build_memory_extraction_chain(
        gateway=StubGateway(response),
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    with pytest.raises(AppError) as error:
        asyncio.run(chain.ainvoke("十轮对话"))

    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert error.value.message == "长期记忆提取结果格式无效"


@pytest.mark.parametrize(
    ("response", "expected_action"),
    [
        (
            '{"action":"create","content":"新承诺。","importance":4}',
            "create",
        ),
        ('{"action":"deduplicate","targetMemoryId":"memory-1"}', "deduplicate"),
        (
            (
                '{"action":"merge","targetMemoryId":"memory-1",'
                '"content":"合并后的承诺。","importance":4}'
            ),
            "merge",
        ),
        (
            (
                '{"action":"update","targetMemoryId":"memory-1",'
                '"content":"更新后的承诺。","importance":5}'
            ),
            "update",
        ),
        ('{"action":"invalidate","targetMemoryId":"memory-1"}', "invalidate"),
        ('{"action":"ignore"}', "ignore"),
    ],
)
def test_memory_consolidation_chain_accepts_six_actions(
    response: str,
    expected_action: str,
) -> None:
    """整合 Chain 只返回六种明确动作之一。"""

    gateway = StubGateway(response)
    chain = build_memory_consolidation_chain(
        gateway=gateway,
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    decision = asyncio.run(chain.ainvoke(build_consolidation_input()))

    assert decision.action == expected_action
    assert '"existingMemories"' in gateway.messages[1]["content"]


@pytest.mark.parametrize(
    "response",
    [
        (
            '{"action":"merge","targetMemoryId":"memory-outside",'
            '"content":"越界合并。","importance":4}'
        ),
        '{"action":"create"}',
        '{"action":"ignore","targetMemoryId":"memory-1"}',
    ],
)
def test_memory_consolidation_chain_rejects_invalid_decisions(response: str) -> None:
    """越界目标和动作字段冲突会让整批整理失败。"""

    chain = build_memory_consolidation_chain(
        gateway=StubGateway(response),
        base_url="https://models.example/v1",
        model="chat-model",
        api_key="secret",
    )

    with pytest.raises(AppError) as error:
        asyncio.run(chain.ainvoke(build_consolidation_input()))

    assert error.value.code == "MODEL_OUTPUT_INVALID"
