/**
 * 页面共用 DTO 与内容块类型。
 * 字段命名对齐《织境-项目架构图》§6 的实体关系图，便于后端 Pydantic DTO 一一映射。
 */

/* ── 消息内容块（PRD §3.5，架构约束 6） ─────────────────────
   助手回复必须按语义拆分为有序内容块；实时展示、历史查看与导出共用同一结构。 */

export type BlockType = 'action' | 'dialogue';

export interface MessageBlock {
  id: string;
  sequence: number;
  type: BlockType;
  content: string;
}

export type MessageRole = 'user' | 'assistant' | 'memo';

export interface Message {
  id: string;
  sessionId: string;
  role: MessageRole;
  blocks: MessageBlock[];
  createdAt: string;
  /** 本轮命中的世界书条目名，仅助手消息可能有 */
  retrieved?: string[];
}

/* ── 用户身份（PRD §3.1，验收 1：仅三个字段） ─────────────── */

export interface UserIdentity {
  id: string;
  /** 姓名：角色在对话中对用户使用的称呼 */
  name: string;
  /** 名称：当前用户身份或人设的名称 */
  personaName: string;
  /** 用户设定 */
  bio: string;
}

export type UpdateUserIdentityPayload = Partial<
  Pick<UserIdentity, 'name' | 'personaName' | 'bio'>
>;

/* ── 角色卡（PRD §3.2，验收 2：仅四个字段） ───────────────── */

export interface DialogueExample {
  user: string;
  assistant: string;
}

export interface Character {
  id: string;
  name: string;
  /** 简介 */
  introduction: string;
  systemPrompt: string;
  dialogueExamples: DialogueExample[];
}

export type UpdateCharacterPayload = Partial<
  Pick<Character, 'name' | 'introduction' | 'systemPrompt' | 'dialogueExamples'>
>;

/* ── 世界书与条目（PRD §3.3） ─────────────────────────────── */

export interface WorldBookEntry {
  id: string;
  worldBookId: string;
  name: string;
  content: string;
  keywords: string[];
  category: string;
  /** 常驻条目每轮固定注入 */
  resident: boolean;
  enabled: boolean;
  /** 内容改动后索引过期，需重新生成 Embedding */
  indexStale?: boolean;
}

export interface WorldBook {
  id: string;
  name: string;
  /** 用户粘贴的完整原文，拆分失败时保留 */
  rawContent: string;
  entries: WorldBookEntry[];
}

/** LLM 拆分产出的草稿条目，未经用户确认不入库、不生成向量 */
export interface WorldBookDraftEntry {
  name: string;
  category: string;
  content: string;
}

/* ── 会话与消息（PRD §3.4） ───────────────────────────────── */

/** 会话未绑定世界书时 worldBookId 为 null，检索层必须短路（架构约束 2） */
export interface Session {
  id: string;
  title: string;
  characterId: string;
  worldBookId: string | null;
  identitySnapshotId: string;
  roundCount: number;
  consolidatedRound: number;
  summary: string;
  createdAt: string;
  updatedAt: string;
}

export interface CreateSessionPayload {
  characterId: string;
  worldBookId: string | null;
}

/* ── 长期记忆（PRD §4.2） ─────────────────────────────────── */

export const MEMORY_TYPES = [
  '用户偏好',
  '角色承诺',
  '关系变化',
  '重要剧情',
  '长期目标',
] as const;

export type MemoryType = (typeof MEMORY_TYPES)[number];

export type MemoryStatus = '有效' | '已失效';

export interface LongTermMemory {
  id: string;
  characterId: string;
  type: MemoryType;
  content: string;
  /** 重要性 1–5 */
  importance: number;
  status: MemoryStatus;
  createdAt: string;
  /** 来源对话 */
  sourceLabel: string;
}

/* ── 模型端点配置（PRD §3.6，架构约束 7） ─────────────────── */

export type ModelGroup = 'main' | 'embed';

/** 服务端返回的配置：只有脱敏尾号，永远不含明文 Key */
export interface ModelEndpointConfig {
  baseUrl: string;
  model: string;
  keySet: boolean;
  keyTail: string;
}

export type ModelSettings = Record<ModelGroup, ModelEndpointConfig>;

/** 提交给服务端的配置：apiKey 留空表示不修改已保存的密钥 */
export interface ModelEndpointPayload {
  baseUrl: string;
  model: string;
  apiKey: string;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
}

/** Embedding 模型或向量维度变更后，索引必须整体重建（PRD §3.6，架构约束 5） */
export interface IndexStatus {
  rebuildRequired: boolean;
}

/* ── 系统日志（PRD §3.8） ─────────────────────────────────── */

export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

export interface LogEntry {
  id: string;
  /** ISO 日期，YYYY-MM-DD */
  date: string;
  time: string;
  level: LogLevel;
  /** 事件所属模块 */
  module: string;
  requestId: string;
  message: string;
}

export type LogRange = 'all' | '1d' | '7d' | '30d';

export interface LogQuery {
  level?: LogLevel | 'all';
  range?: LogRange;
}

/* ── 对话流式事件（services/chatStream.ts） ───────────────── */

export type ChatStreamEvent =
  /** 本轮命中的世界书条目 */
  | { type: 'retrieval'; entries: string[] }
  | { type: 'block_start'; sequence: number; blockType: BlockType }
  | { type: 'block_delta'; sequence: number; text: string }
  | { type: 'block_end'; sequence: number }
  /** 达到 10 轮时触发的长期记忆整理提示 */
  | { type: 'memo'; text: string }
  | { type: 'done'; messageId: string; roundCount: number }
  | { type: 'error'; message: string };
