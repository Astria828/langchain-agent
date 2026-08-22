/**
 * REST 请求封装与统一错误处理。
 *
 * 接口划分对齐后端三个路由文件（《织境-项目文件架构图》§1）：
 *   /api/identity, /api/characters, /api/worldbooks  → backend/app/api/content.py
 *   /api/sessions, /api/memories                     → backend/app/api/chat.py
 *   /api/settings, /api/index, /api/logs, /api/export → backend/app/api/system.py
 *
 * 安全约定（PRD §3.6，架构约束 7）：API Key 只出站不回显。
 * ModelEndpointConfig 里不存在明文 key 字段，服务端也不得返回。
 */

import type {
  Character,
  ConnectionTestResult,
  CreateSessionPayload,
  IndexStatus,
  LogEntry,
  LogQuery,
  LongTermMemory,
  Message,
  RecommendedReply,
  MemoryStatus,
  MemoryType,
  ModelEndpointPayload,
  ModelGroup,
  ModelSettings,
  Session,
  UpdateCharacterPayload,
  UpdateUserIdentityPayload,
  UserIdentity,
  WorldBook,
  WorldBookDraftEntry,
  WorldBookEntry,
} from '@/types';

const BASE = '/api';

export class ApiError extends Error {
  readonly status: number;
  readonly requestId?: string;

  constructor(message: string, status: number, requestId?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.requestId = requestId;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      headers: {
        ...(init?.body ? { 'Content-Type': 'application/json' } : null),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError('无法连接到后端服务，请确认服务已启动', 0);
  }

  if (res.status === 204) return undefined as T;

  const payload = await res.json().catch(() => null);

  if (!res.ok) {
    // 后端异常已脱敏（backend/app/core/exceptions.py），这里原样透出
    const message =
      (payload && (payload.message || payload.detail)) || `请求失败（${res.status}）`;
    throw new ApiError(message, res.status, payload?.requestId);
  }

  // 统一响应壳 { data: ... }；后端若直接返回裸对象也能兼容
  return payload && typeof payload === 'object' && 'data' in payload ? payload.data : payload;
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

/** 前端全部数据访问的契约，mock 与真实实现共用 */
export interface ApiClient {
  /* 身份 */
  getIdentity(): Promise<UserIdentity>;
  updateIdentity(patch: UpdateUserIdentityPayload): Promise<UserIdentity>;

  /* 角色卡 */
  listCharacters(): Promise<Character[]>;
  createCharacter(): Promise<Character>;
  updateCharacter(id: string, patch: UpdateCharacterPayload): Promise<Character>;
  deleteCharacter(id: string): Promise<void>;

  /* 世界书 */
  listWorldBooks(): Promise<WorldBook[]>;
  createWorldBook(): Promise<WorldBook>;
  updateWorldBook(id: string, patch: Partial<Pick<WorldBook, 'name' | 'rawContent'>>): Promise<WorldBook>;
  deleteWorldBook(id: string): Promise<void>;
  /** LLM 仅整理与拆分原文，不扩写、不推测、不修改设定（PRD §3.3） */
  splitWorldBook(id: string): Promise<WorldBookDraftEntry[]>;
  /** 草稿经用户确认后才正式入库并生成 Embedding */
  confirmDrafts(id: string, drafts: WorldBookDraftEntry[]): Promise<WorldBookEntry[]>;
  createEntry(bookId: string): Promise<WorldBookEntry>;
  updateEntry(bookId: string, entryId: string, patch: Partial<WorldBookEntry>): Promise<WorldBookEntry>;
  deleteEntry(bookId: string, entryId: string): Promise<void>;
  /** 为索引过期的条目重新生成向量 */
  reembed(bookId: string): Promise<{ count: number }>;

  /* 会话与消息 */
  listSessions(): Promise<Session[]>;
  createSession(payload: CreateSessionPayload): Promise<Session>;
  updateSession(id: string, patch: Partial<Pick<Session, 'title'>>): Promise<Session>;
  deleteSession(id: string): Promise<void>;
  listMessages(sessionId: string): Promise<Message[]>;
  deleteTurn(sessionId: string, assistantMessageId: string): Promise<void>;
  /** 单独删除一条没能等到回复的断层用户消息 */
  deleteMessage(sessionId: string, messageId: string): Promise<void>;
  recommendedReply(sessionId: string): Promise<RecommendedReply>;

  /* 长期记忆 */
  listMemories(query?: {
    characterId?: string;
    type?: MemoryType;
    status?: MemoryStatus;
  }): Promise<LongTermMemory[]>;
  invalidateMemory(id: string): Promise<LongTermMemory>;

  /* 模型配置 */
  getModelSettings(): Promise<ModelSettings>;
  testConnection(group: ModelGroup, payload: ModelEndpointPayload): Promise<ConnectionTestResult>;
  saveModelSettings(group: ModelGroup, payload: ModelEndpointPayload): Promise<ModelSettings>;
  getIndexStatus(): Promise<IndexStatus>;
  rebuildIndex(): Promise<IndexStatus>;

  /* 日志 */
  listLogs(query: LogQuery): Promise<LogEntry[]>;
  deleteLogs(query: LogQuery): Promise<{ count: number }>;
  downloadLogs(query: LogQuery): Promise<Blob>;

  /* 导出（只读，不修改会话状态、整理进度或向量索引 —— PRD §3.7） */
  exportSession(sessionId: string): Promise<Blob>;
  exportAll(): Promise<Blob>;
}

const qs = (params: Record<string, string | undefined>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? `?${s}` : '';
};

const download = async (path: string): Promise<Blob> => {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new ApiError(`导出失败（${res.status}）`, res.status);
  return res.blob();
};

export const realApi: ApiClient = {
  getIdentity: () => request('/identity'),
  updateIdentity: (patch) => request('/identity', { method: 'PUT', ...json(patch) }),

  listCharacters: () => request('/characters'),
  createCharacter: () => request('/characters', { method: 'POST', ...json({}) }),
  updateCharacter: (id, patch) => request(`/characters/${id}`, { method: 'PUT', ...json(patch) }),
  deleteCharacter: (id) => request(`/characters/${id}`, { method: 'DELETE' }),

  listWorldBooks: () => request('/worldbooks'),
  createWorldBook: () => request('/worldbooks', { method: 'POST', ...json({}) }),
  updateWorldBook: (id, patch) => request(`/worldbooks/${id}`, { method: 'PUT', ...json(patch) }),
  deleteWorldBook: (id) => request(`/worldbooks/${id}`, { method: 'DELETE' }),
  splitWorldBook: (id) => request(`/worldbooks/${id}/split`, { method: 'POST', ...json({}) }),
  confirmDrafts: (id, drafts) =>
    request(`/worldbooks/${id}/entries/confirm`, { method: 'POST', ...json({ drafts }) }),
  createEntry: (bookId) => request(`/worldbooks/${bookId}/entries`, { method: 'POST', ...json({}) }),
  updateEntry: (bookId, entryId, patch) =>
    request(`/worldbooks/${bookId}/entries/${entryId}`, { method: 'PUT', ...json(patch) }),
  deleteEntry: (bookId, entryId) =>
    request(`/worldbooks/${bookId}/entries/${entryId}`, { method: 'DELETE' }),
  reembed: (bookId) => request(`/worldbooks/${bookId}/reembed`, { method: 'POST', ...json({}) }),

  listSessions: () => request('/sessions'),
  createSession: (payload) => request('/sessions', { method: 'POST', ...json(payload) }),
  updateSession: (id, patch) => request(`/sessions/${id}`, { method: 'PATCH', ...json(patch) }),
  deleteSession: (id) => request(`/sessions/${id}`, { method: 'DELETE' }),
  listMessages: (sessionId) => request(`/sessions/${sessionId}/messages`),
  deleteTurn: (sessionId, assistantMessageId) =>
    request(`/sessions/${sessionId}/turns/${assistantMessageId}`, { method: 'DELETE' }),
  deleteMessage: (sessionId, messageId) =>
    request(`/sessions/${sessionId}/messages/${messageId}`, { method: 'DELETE' }),
  recommendedReply: (sessionId) =>
    request(`/sessions/${sessionId}/recommended-reply`, { method: 'POST' }),

  listMemories: (query) =>
    request(
      `/memories${qs({ characterId: query?.characterId, type: query?.type, status: query?.status })}`,
    ),
  invalidateMemory: (id) => request(`/memories/${id}/invalidate`, { method: 'POST', ...json({}) }),

  getModelSettings: () => request('/settings/models'),
  testConnection: (group, payload) =>
    request(`/settings/models/${group}/test`, { method: 'POST', ...json(payload) }),
  saveModelSettings: (group, payload) =>
    request(`/settings/models/${group}`, { method: 'PUT', ...json(payload) }),
  getIndexStatus: () => request('/index/status'),
  rebuildIndex: () => request('/index/rebuild', { method: 'POST', ...json({}) }),

  listLogs: (query) => request(`/logs${qs({ level: query.level, range: query.range })}`),
  deleteLogs: (query) =>
    request(`/logs${qs({ level: query.level, range: query.range })}`, { method: 'DELETE' }),
  downloadLogs: (query) => download(`/logs/download${qs({ level: query.level, range: query.range })}`),

  exportSession: (sessionId) => download(`/export/sessions/${sessionId}`),
  exportAll: () => download('/export/all'),
};

/** 阶段 9 起九个页面固定访问真实 REST API，聊天流固定访问真实 SSE。 */
export const client: ApiClient = realApi;
