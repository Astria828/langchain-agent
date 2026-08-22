"""OpenAI-compatible 模型网关的请求与脱敏错误测试。"""

import asyncio
import json

import httpx
import pytest
from app.core.exceptions import AppError
from app.gateways.model_gateway import (
    ModelGateway,
    ModelStreamChunk,
    normalize_base_url,
)


def stream_response(*contents: str) -> httpx.Response:
    """构造 OpenAI-compatible SSE 测试响应。"""

    frames = [
        f"data: {json.dumps({'choices': [{'delta': {'content': content}}]}, ensure_ascii=False)}\n\n"
        for content in contents
    ]
    return httpx.Response(200, text="".join([*frames, "data: [DONE]\n\n"]))


def test_main_connection_uses_openai_compatible_contract() -> None:
    """主模型测试发送最小流式聊天请求和 Bearer 凭据。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return stream_response("ok")

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
            "max_tokens": 512,
            "stream": True,
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


def test_chat_completion_sends_response_format_when_requested() -> None:
    """非流式调用同样支持结构化输出约束，不传时请求体里不出现该字段。"""

    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"content": "走吧"}'}}]},
        )

    response_format = {"type": "json_schema", "json_schema": {"name": "draft"}}
    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    asyncio.run(
        gateway.create_chat_completion(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
            messages=[{"role": "user", "content": "原文"}],
            response_format=response_format,
        )
    )
    asyncio.run(
        gateway.create_chat_completion(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
            messages=[{"role": "user", "content": "原文"}],
        )
    )

    assert captured[0]["response_format"] == response_format
    assert "response_format" not in captured[1]


def test_chat_completion_falls_back_when_response_format_rejected() -> None:
    """上游拒绝 response_format 时去掉该字段重试，而不是整次调用失败。"""

    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": {"message": "unsupported"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"content": "走吧"}'}}]},
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    content = asyncio.run(
        gateway.create_chat_completion(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
            messages=[{"role": "user", "content": "原文"}],
            response_format={"type": "json_schema", "json_schema": {"name": "draft"}},
        )
    )

    assert len(captured) == 2
    assert "response_format" not in captured[1]
    assert content == '{"content": "走吧"}'


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


def test_chat_completion_stream_returns_only_ordered_content_deltas() -> None:
    """角色对话忽略角色和 usage 帧，只按顺序交付非空正文增量。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        frames = [
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"前半"}}]}\n\n',
            'data: {"choices":[]}\n\n',
            'data: {"choices":[{"delta":{"content":"后半"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
            )
        ]

    assert asyncio.run(collect()) == [
        ModelStreamChunk("content", "前半"),
        ModelStreamChunk("content", "后半"),
    ]
    assert captured["payload"] == {
        "model": "chat-model",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": True,
    }


def test_chat_completion_stream_sends_temperature_only_when_requested() -> None:
    """显式传入采样温度时才写入请求体，供抽取类调用固定为确定性输出。"""

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        frames = [
            'data: {"choices":[{"delta":{"content":"正文"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[str]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
                temperature=0,
            )
        ]

    assert asyncio.run(collect()) == [ModelStreamChunk("content", "正文")]
    assert captured["payload"] == {
        "model": "chat-model",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": True,
        "temperature": 0,
    }


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
        return stream_response("ok")

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    asyncio.run(
        gateway.test_main(
            base_url="https://models.example/v1",
            model="chat-model",
            api_key="sk-test-main",
        )
    )
    assert attempts == 2


def test_timeout_is_retried_once_and_returns_stable_error() -> None:
    """连续超时只重试一次，并转换为不含请求细节的稳定 504 错误。"""

    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("upstream timeout with secret", request=request)

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(
            gateway.test_main(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-timeout-secret",
            )
        )

    assert attempts == 2
    assert error.value.status_code == 504
    assert error.value.code == "MODEL_CONNECTION_TIMEOUT"
    assert "secret" not in error.value.message


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


def test_chat_completion_stream_forwards_reasoning_before_content() -> None:
    """推理增量按上游顺序转发，且与正文严格区分。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        frames = [
            'data: {"choices":[{"delta":{"reasoning":"先想一下"}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":"再想一下"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"正文"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[ModelStreamChunk]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
            )
        ]

    assert asyncio.run(collect()) == [
        ModelStreamChunk("reasoning", "先想一下"),
        ModelStreamChunk("reasoning", "再想一下"),
        ModelStreamChunk("content", "正文"),
    ]


def test_main_connection_accepts_reasoning_only_response() -> None:
    """推理模型可能在测试额度内只输出思维链，这仍然算连接可用。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        frames = [
            'data: {"choices":[{"delta":{"reasoning":"先想一下"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    asyncio.run(
        gateway.test_main(
            base_url="https://models.example/v1",
            model="reasoning-model",
            api_key="sk-test-main",
        )
    )


def test_main_connection_rejects_empty_response() -> None:
    """一个增量都没有时仍判定为主模型输出无效。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n")

    gateway = ModelGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(
            gateway.test_main(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
            )
        )
    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_chat_completion_stream_sends_response_format_when_requested() -> None:
    """结构化输出约束只在显式传入时下发，不影响其他调用方的请求体。"""

    captured: dict[str, object] = {}
    response_format = {"type": "json_schema", "json_schema": {"name": "chat_reply"}}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        frames = [
            'data: {"choices":[{"delta":{"content":"正文"}}]}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[ModelStreamChunk]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
                max_tokens=8192,
                response_format=response_format,
            )
        ]

    assert asyncio.run(collect()) == [ModelStreamChunk("content", "正文")]
    assert captured["payload"] == {
        "model": "chat-model",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": True,
        "max_tokens": 8192,
        "response_format": response_format,
    }


def test_chat_completion_stream_retries_without_unsupported_response_format() -> None:
    """上游拒绝结构化输出字段时退回纯提示词模式，不能让整轮对话失败。"""

    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": {"message": "unsupported parameter"}})
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"正文"}}]}\n\ndata: [DONE]\n\n',
        )

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[ModelStreamChunk]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
                response_format={"type": "json_schema", "json_schema": {"name": "chat_reply"}},
            )
        ]

    assert asyncio.run(collect()) == [ModelStreamChunk("content", "正文")]
    assert len(payloads) == 2
    assert "response_format" in payloads[0]
    assert "response_format" not in payloads[1]


def test_chat_completion_stream_reports_mid_stream_upstream_error() -> None:
    """HTTP 200 流中间的错误帧按调用失败上报，不能伪装成模型格式错误。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        frames = [
            'data: {"choices":[{"delta":{"content":"开头"}}]}\n\n',
            'data: {"error":{"code":429,"message":"Rate limit exceeded"}}\n\n',
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[ModelStreamChunk]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key="sk-test-main",
                messages=[{"role": "user", "content": "你好"}],
            )
        ]

    with pytest.raises(AppError) as error:
        asyncio.run(collect())

    assert error.value.code == "MODEL_CONNECTION_FAILED"
    assert "Rate limit exceeded" in error.value.message


def test_upstream_error_body_never_echoes_credentials() -> None:
    """错误正文里的 Key 与鉴权失败描述都不能进入业务错误。"""

    secret = "sk-sensitive-upstream-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        frames = [
            f'data: {{"error":{{"code":401,"message":"invalid key {secret}"}}}}\n\n',
        ]
        return httpx.Response(200, text="".join(frames))

    gateway = ModelGateway(transport=httpx.MockTransport(handler))

    async def collect() -> list[ModelStreamChunk]:
        return [
            chunk
            async for chunk in gateway.stream_chat_completion(
                base_url="https://models.example/v1",
                model="chat-model",
                api_key=secret,
                messages=[{"role": "user", "content": "你好"}],
            )
        ]

    with pytest.raises(AppError) as error:
        asyncio.run(collect())

    assert error.value.code == "MODEL_CONNECTION_FAILED"
    assert secret not in error.value.message
    assert "invalid key" not in error.value.message


def test_non_streaming_call_rejects_error_body_returned_with_http_200() -> None:
    """网关在 200 响应体里回报的上游错误不能被当成可解析结果。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "Insufficient credits"}})

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

    assert error.value.code == "MODEL_CONNECTION_FAILED"
    assert "Insufficient credits" in error.value.message
