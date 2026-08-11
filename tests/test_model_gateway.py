"""OpenAI-compatible 模型网关的请求与脱敏错误测试。"""

import asyncio
import json

import httpx
import pytest
from app.core.exceptions import AppError
from app.gateways.model_gateway import ModelGateway, normalize_base_url


def test_main_connection_uses_openai_compatible_contract() -> None:
    """主模型测试发送最小聊天请求和 Bearer 凭据。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    asyncio.run(
        gateway.test_main(
            base_url="https://models.example/v1/",
            model="chat-model",
            api_key="sk-test-main",
        )
    )

    assert captured == {
        "path": "/v1/chat/completions",
        "authorization": "Bearer sk-test-main",
        "payload": {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        },
    }


def test_embedding_connection_returns_actual_dimension() -> None:
    """Embedding 测试从真实响应向量计算维度。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    dimension = asyncio.run(
        gateway.test_embedding(
            base_url="https://models.example/v1",
            model="embed-model",
            api_key="sk-test-embed",
        )
    )

    assert dimension == 3


def test_chat_completion_returns_validated_message_content() -> None:
    """正式主模型调用使用确定温度，并只返回非空文本内容。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"drafts": []}'}}]},
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    content = asyncio.run(
        gateway.create_chat_completion(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
            messages=[
                {"role": "system", "content": "规则"},
                {"role": "user", "content": "原文"},
            ],
        )
    )

    assert captured["payload"] == {
        "model": "chat-model",
        "messages": [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "原文"},
        ],
        "temperature": 0,
    }
    assert content == '{"drafts": []}'


def test_chat_completion_rejects_missing_message_content() -> None:
    """主模型没有返回可用文本时转换为稳定结构化输出错误。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(
            gateway.create_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "原文"}],
            )
        )

    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_embedding_batch_uses_openai_contract_and_validates_dimension() -> None:
    """重建调用一次提交文本数组，并校验每条向量与配置维度一致。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                ]
            },
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    embeddings = asyncio.run(
        gateway.create_embeddings(
            base_url="https://models.example/v1",
            model="embed-model",
            api_key="sk-test-embed",
            texts=["条目一", "条目二"],
            expected_dimension=3,
        )
    )

    assert captured["payload"] == {
        "model": "embed-model",
        "input": ["条目一", "条目二"],
    }
    assert embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_embedding_batch_rejects_mismatched_dimension() -> None:
    """服务返回的向量维度变化时不能写入当前版本 Collection。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(
            gateway.create_embeddings(
                base_url="https://models.example/v1",
                model="embed-model",
                api_key="sk-test-embed",
                texts=["条目"],
                expected_dimension=3,
            )
        )

    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_transient_upstream_status_is_retried_once() -> None:
    """429/502/503/504 等真实瞬时故障只重试一次。"""

    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="temporary")
        return httpx.Response(200, json={"choices": [{}]})

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    asyncio.run(
        gateway.test_main(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
        )
    )
    assert attempts == 2


def test_upstream_error_does_not_expose_response_or_key() -> None:
    """服务商响应正文和 API Key 不进入业务错误。"""

    secret = "sk-sensitive-upstream-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid key {secret}")

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(
            gateway.test_main(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key=secret,
            )
        )

    assert error.value.code == "MODEL_CONNECTION_FAILED"
    assert secret not in error.value.message
    assert "invalid key" not in error.value.message


@pytest.mark.parametrize(
    "base_url",
    ["models.example/v1", "ftp://models.example/v1", "https://models.example/v1?key=x"],
)
def test_base_url_rejects_unsupported_or_secret_bearing_urls(base_url: str) -> None:
    """Base URL 只允许不含查询参数的 HTTP(S) 地址。"""

    with pytest.raises(ValueError):
        normalize_base_url(base_url)
