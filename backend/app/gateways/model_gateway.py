"""OpenAI-compatible 主模型与 Embedding 连接测试网关。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from json import JSONDecodeError, loads
from typing import Literal, NamedTuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.exceptions import AppError
from app.core.logging import sanitize_log_text

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
# 不认识 response_format 的上游会直接拒绝整个请求，此时退回无结构化约束再试一次，
# 避免为了兼容弱模型反而让原本正常的服务不可用。
STRUCTURED_OUTPUT_REJECTED_CODES = {400, 404, 415, 422}
MAX_ATTEMPTS = 2
# 连接测试的生成额度：推理模型要先输出完思维链才会进入正文，给小了必然被截断。
CONNECTION_TEST_MAX_TOKENS = 512
RETRY_DELAY_SECONDS = 0.25
UPSTREAM_ERROR_MESSAGE_LIMIT = 200
# 鉴权失败的响应正文最可能把提交的凭据原样回显，这类错误一律不转述上游描述。
CREDENTIAL_SENSITIVE_STATUS_CODES = {401, 403}


def _error_suffix(response: httpx.Response) -> str:
    """尽力从失败响应体中补出上游错误描述，读不出来或涉及凭据就返回空串。"""

    if response.status_code in CREDENTIAL_SENSITIVE_STATUS_CODES:
        return ""
    try:
        payload = response.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = upstream_error_message(payload)
    return f"：{message}" if message else ""


def upstream_error_message(payload: dict[str, object]) -> str | None:
    """从 OpenAI 兼容响应体中取出上游错误描述，没有错误则返回 None。"""

    error = payload.get("error")
    if error is None:
        return None
    if isinstance(error, dict):
        if error.get("code") in CREDENTIAL_SENSITIVE_STATUS_CODES:
            return "上游鉴权失败"
        detail = error.get("message") or error.get("code") or error.get("type")
    else:
        detail = error
    text = " ".join(sanitize_log_text(detail if detail is not None else error).split())
    if not text:
        return "上游未提供错误描述"
    if len(text) > UPSTREAM_ERROR_MESSAGE_LIMIT:
        return f"{text[:UPSTREAM_ERROR_MESSAGE_LIMIT]}…"
    return text


class ModelStreamChunk(NamedTuple):
    """主模型 SSE 的一个增量：正文或推理过程。"""

    kind: Literal["content", "reasoning"]
    text: str


def normalize_base_url(base_url: str) -> str:
    """校验并标准化不含查询参数的 HTTP(S) Base URL。"""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP(S) 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不能包含查询参数或片段")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


class ModelGateway:
    """执行不记录 Prompt 或响应正文的最小模型调用。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def test_main(self, *, base_url: str, model: str, api_key: str) -> None:
        """发送一条最小流式聊天请求并验证主模型支持 SSE。"""

        # 推理模型先把额度花在思维链上：token 给少了就只剩推理增量、正文永远等不到，
        # 所以连接测试既要给够额度，也要把推理增量本身算作"这条 SSE 通了"。
        received_any = False
        async for chunk in self.stream_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=CONNECTION_TEST_MAX_TOKENS,
        ):
            if chunk.text:
                received_any = True
        if not received_any:
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="主模型返回格式无效",
            )

    async def test_embedding(self, *, base_url: str, model: str, api_key: str) -> int:
        """生成测试向量并返回服务端实际向量维度。"""

        embeddings = await self.create_embeddings(
            base_url=base_url,
            model=model,
            api_key=api_key,
            texts=["LoreWeave connection test"],
        )
        return len(embeddings[0])

    async def create_chat_completion(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        messages: list[dict[str, str]],
        response_format: dict[str, object] | None = None,
    ) -> str:
        """调用主模型并返回经过结构校验的文本内容。"""

        json_body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        if response_format is not None:
            json_body["response_format"] = response_format

        payload = await self._post(
            url=f"{normalize_base_url(base_url)}/chat/completions",
            api_key=api_key,
            json_body=json_body,
            model=model,
        )
        choices = payload.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else None
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="主模型返回格式无效",
            )
        return content

    async def stream_chat_completion(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> AsyncIterator[ModelStreamChunk]:
        """调用主模型 SSE，并按上游顺序返回推理与正文增量。"""

        # 角色回复默认不固定采样温度：省略该字段由上游取模型默认值，
        # 避免贪心解码让重说与继续说反复得到同一段文本。
        json_body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if max_tokens is not None:
            json_body["max_tokens"] = max_tokens
        if temperature is not None:
            json_body["temperature"] = temperature
        if response_format is not None:
            json_body["response_format"] = response_format

        url = f"{normalize_base_url(base_url)}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                emitted_any = False
                try:
                    async with client.stream(
                        "POST",
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=json_body,
                    ) as response:
                        if (
                            response.status_code in TRANSIENT_STATUS_CODES
                            and attempt + 1 < MAX_ATTEMPTS
                        ):
                            await asyncio.sleep(RETRY_DELAY_SECONDS)
                            continue
                        if not response.is_success:
                            # 上游拒绝的可能只是 response_format 这一个字段，
                            # 去掉结构化约束重试一次，弱模型的兼容手段不能拖垮强模型。
                            if (
                                "response_format" in json_body
                                and response.status_code in STRUCTURED_OUTPUT_REJECTED_CODES
                                and attempt + 1 < MAX_ATTEMPTS
                            ):
                                json_body.pop("response_format")
                                logger.warning(
                                    "上游拒绝结构化输出约束，退回纯提示词模式 model=%s status=%s",
                                    model,
                                    response.status_code,
                                    extra={
                                        "event": "model_structured_output_unsupported",
                                        "business_ids": {
                                            "model": model,
                                            "statusCode": str(response.status_code),
                                        },
                                    },
                                )
                                continue
                            raise AppError(
                                status_code=502,
                                code="MODEL_CONNECTION_FAILED",
                                message=f"模型服务调用失败（HTTP {response.status_code}）",
                            )

                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data:
                                continue
                            if data == "[DONE]":
                                return
                            try:
                                payload = loads(data)
                            except JSONDecodeError as exc:
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_OUTPUT_INVALID",
                                    message="模型服务返回了无效流式 JSON",
                                ) from exc
                            if not isinstance(payload, dict):
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_OUTPUT_INVALID",
                                    message="模型服务返回格式无效",
                                )

                            # 上游可以在 HTTP 200 的流中间推送错误帧（限流、供应商掉线、
                            # 内容过滤）。这类帧没有 choices，必须按调用失败上报，
                            # 否则会被误判成模型不会按格式输出。
                            error_message = upstream_error_message(payload)
                            if error_message is not None:
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_CONNECTION_FAILED",
                                    message=f"模型服务返回错误：{error_message}",
                                )

                            choices = payload.get("choices")
                            if not isinstance(choices, list):
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_OUTPUT_INVALID",
                                    message="模型服务返回格式无效",
                                )
                            if not choices:
                                continue
                            first_choice = choices[0]
                            delta = (
                                first_choice.get("delta")
                                if isinstance(first_choice, dict)
                                else None
                            )
                            if not isinstance(delta, dict):
                                continue

                            # 推理增量先于正文到达，转发出去让上层能展示思考状态。
                            # 两种字段名是同一个东西的不同厂商拼写，取其一即可。
                            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                            if isinstance(reasoning, str) and reasoning:
                                emitted_any = True
                                yield ModelStreamChunk("reasoning", reasoning)

                            content = delta.get("content")
                            if content is None or content == "":
                                continue
                            if not isinstance(content, str):
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_OUTPUT_INVALID",
                                    message="模型服务返回格式无效",
                                )
                            emitted_any = True
                            yield ModelStreamChunk("content", content)
                        return
                except httpx.TimeoutException as exc:
                    if not emitted_any and attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    raise AppError(
                        status_code=504,
                        code="MODEL_CONNECTION_TIMEOUT",
                        message="模型服务连接超时",
                    ) from exc
                except httpx.RequestError as exc:
                    if not emitted_any and attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    raise AppError(
                        status_code=502,
                        code="MODEL_CONNECTION_FAILED",
                        message="无法连接模型服务",
                    ) from exc

    async def create_embeddings(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        texts: list[str],
        expected_dimension: int | None = None,
    ) -> list[list[float]]:
        """批量生成向量，并校验数量、数值类型与配置维度。"""

        if not texts:
            return []

        payload = await self._post(
            url=f"{normalize_base_url(base_url)}/embeddings",
            api_key=api_key,
            json_body={"model": model, "input": texts},
            model=model,
        )
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise AppError(
                status_code=502,
                code="MODEL_OUTPUT_INVALID",
                message="Embedding 服务返回格式无效",
            )

        embeddings: list[list[float]] = []
        for item in data:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if (
                not isinstance(embedding, list)
                or not embedding
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in embedding
                )
                or (expected_dimension is not None and len(embedding) != expected_dimension)
            ):
                raise AppError(
                    status_code=502,
                    code="MODEL_OUTPUT_INVALID",
                    message="Embedding 服务返回格式或向量维度无效",
                )
            embeddings.append([float(value) for value in embedding])
        return embeddings

    async def _post(
        self,
        *,
        url: str,
        api_key: str,
        json_body: dict[str, object],
        model: str,
    ) -> dict[str, object]:
        """对真实瞬时故障重试一次，并把上游错误转换为脱敏业务错误。"""

        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=json_body,
                    )
                except httpx.TimeoutException as exc:
                    if attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    raise AppError(
                        status_code=504,
                        code="MODEL_CONNECTION_TIMEOUT",
                        message="模型服务连接超时",
                    ) from exc
                except httpx.RequestError as exc:
                    if attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    raise AppError(
                        status_code=502,
                        code="MODEL_CONNECTION_FAILED",
                        message="无法连接模型服务",
                    ) from exc

                if response.status_code in TRANSIENT_STATUS_CODES and attempt + 1 < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                if not response.is_success:
                    # 与 SSE 通道保持一致：上游只是不认识 response_format 时，
                    # 去掉这一个字段重试，而不是让整次调用失败。
                    if (
                        "response_format" in json_body
                        and response.status_code in STRUCTURED_OUTPUT_REJECTED_CODES
                        and attempt + 1 < MAX_ATTEMPTS
                    ):
                        json_body.pop("response_format")
                        logger.warning(
                            "上游拒绝结构化输出约束，退回纯提示词模式 model=%s status=%s",
                            model,
                            response.status_code,
                            extra={
                                "event": "model_structured_output_unsupported",
                                "business_ids": {
                                    "model": model,
                                    "statusCode": str(response.status_code),
                                },
                            },
                        )
                        continue
                    raise AppError(
                        status_code=502,
                        code="MODEL_CONNECTION_FAILED",
                        message=(
                            f"模型服务调用失败（HTTP {response.status_code}）"
                            f"{_error_suffix(response)}"
                        ),
                    )

                try:
                    payload = response.json()
                except JSONDecodeError as exc:
                    raise AppError(
                        status_code=502,
                        code="MODEL_OUTPUT_INVALID",
                        message="模型服务返回了无效 JSON",
                    ) from exc
                if not isinstance(payload, dict):
                    raise AppError(
                        status_code=502,
                        code="MODEL_OUTPUT_INVALID",
                        message="模型服务返回格式无效",
                    )
                # 部分网关在 HTTP 200 的响应体里回报上游错误，不能当成正常结果继续解析。
                error_message = upstream_error_message(payload)
                if error_message is not None:
                    raise AppError(
                        status_code=502,
                        code="MODEL_CONNECTION_FAILED",
                        message=f"模型服务返回错误：{error_message}",
                    )
                return payload

        raise RuntimeError("模型连接测试未产生结果")
