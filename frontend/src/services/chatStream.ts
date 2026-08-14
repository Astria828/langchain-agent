/**
 * 连接聊天 SSE 并接收 action / dialogue 内容块。
 *
 * 用 fetch + ReadableStream 而非 EventSource：本接口需要 POST body 传用户输入，
 * EventSource 只支持 GET。
 *
 * 事件契约与后端 backend/app/api/chat.py 的流式响应对应：
 *   retrieval / block_start / block_delta / block_end / memo / done / error
 */

import type { ChatStreamEvent, MessageAction } from '@/types';

export type ChatStreamHandler = (event: ChatStreamEvent) => void;

async function streamRequest(
  path: string,
  body: unknown,
  onEvent: ChatStreamHandler,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      ...(body === undefined ? null : { body: JSON.stringify(body) }),
      signal,
    });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return;
    onEvent({ type: 'error', message: '无法连接到后端服务，请确认服务已启动' });
    return;
  }

  if (!res.ok || !res.body) {
    let message = `生成失败（${res.status}）`;
    try {
      const body = (await res.json()) as { message?: string };
      if (body.message) message = body.message;
    } catch {
      // 非 JSON 错误响应保留 HTTP 状态，避免掩盖真实状态码
    }
    onEvent({ type: 'error', message });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 以空行分隔事件
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const payload = chunk
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('');
        if (payload && payload !== '[DONE]') {
          try {
            onEvent(JSON.parse(payload) as ChatStreamEvent);
          } catch {
            // 单个事件解析失败不中断整条流，避免丢失后续内容块
            onEvent({ type: 'error', message: '收到无法解析的流式事件' });
          }
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
  } catch (err) {
    if ((err as Error)?.name !== 'AbortError') {
      onEvent({ type: 'error', message: '流式连接中断' });
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * 发送一条用户消息并接收真实后端的流式角色回复。
 * 会话域已进入阶段 5，不再使用模拟剧本。
 */
export const streamChat = (
  sessionId: string,
  text: string,
  onEvent: ChatStreamHandler,
  signal?: AbortSignal,
) => streamRequest(`/api/sessions/${sessionId}/messages`, { content: text }, onEvent, signal);

/** 对最后一条助手回复执行重说或继续说，并复用同一 SSE 事件契约。 */
export const streamMessageAction = (
  sessionId: string,
  messageId: string,
  action: MessageAction,
  onEvent: ChatStreamHandler,
  signal?: AbortSignal,
) =>
  streamRequest(
    `/api/sessions/${sessionId}/messages/${messageId}/${action}`,
    undefined,
    onEvent,
    signal,
  );
