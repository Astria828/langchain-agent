"""OpenAI-compatible 主模型与 Embedding 连接测试网关。"""

import asyncio
from collections.abc import AsyncIterator
from json import JSONDecodeError, loads
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.exceptions import AppError

TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 0.25


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

        received_content = False
        async for content in self.stream_chat_completion(
            base_url=base_url,
            model=model,
            api_key=api_key,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        ):
            if content:
                received_content = True
        if not received_content:
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
    ) -> str:
        """调用主模型并返回经过结构校验的文本内容。"""

        payload = await self._post(
            url=f"{normalize_base_url(base_url)}/chat/completions",
            api_key=api_key,
            json_body={
                "model": model,
                "messages": messages,
                "temperature": 0,
            },
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
    ) -> AsyncIterator[str]:
        """调用主模型 SSE，并按上游顺序返回正文增量。"""

        json_body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "stream": True,
        }
        if max_tokens is not None:
            json_body["max_tokens"] = max_tokens

        url = f"{normalize_base_url(base_url)}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            for attempt in range(MAX_ATTEMPTS):
                emitted_content = False
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
                            content = delta.get("content") if isinstance(delta, dict) else None
                            if content is None or content == "":
                                continue
                            if not isinstance(content, str):
                                raise AppError(
                                    status_code=502,
                                    code="MODEL_OUTPUT_INVALID",
                                    message="模型服务返回格式无效",
                                )
                            emitted_content = True
                            yield content
                        return
                except httpx.TimeoutException as exc:
                    if not emitted_content and attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue
                    raise AppError(
                        status_code=504,
                        code="MODEL_CONNECTION_TIMEOUT",
                        message="模型服务连接超时",
                    ) from exc
                except httpx.RequestError as exc:
                    if not emitted_content and attempt + 1 < MAX_ATTEMPTS:
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
                    raise AppError(
                        status_code=502,
                        code="MODEL_CONNECTION_FAILED",
                        message=f"模型服务调用失败（HTTP {response.status_code}）",
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
                return payload

        raise RuntimeError("模型连接测试未产生结果")
