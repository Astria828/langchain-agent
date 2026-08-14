/**
 * Mock 适配层：实现与 services/api.ts 中 realApi 完全相同的 ApiClient 契约，
 * 数据落 localStorage，让九个页面在后端就位前即可完整跑通。
 *
 * 后端落地后在 .env 设置 VITE_USE_MOCK=false 切换，页面代码不需要任何改动。
 */

import type { ApiClient } from '../api';
import type {
  Character,
  ChatStreamEvent,
  ConnectionTestResult,
  CreateSessionPayload,
  IndexStatus,
  LogEntry,
  LogQuery,
  LongTermMemory,
  Message,
  ModelEndpointPayload,
  ModelGroup,
  ModelSettings,
  Session,
  UserIdentity,
  WorldBook,
  WorldBookDraftEntry,
  WorldBookEntry,
} from '@/types';
import {
  MOCK_TODAY,
  seedCharacters,
  seedIdentity,
  seedLogs,
  seedMemories,
  seedMessages,
  seedModelSettings,
  seedReplies,
  seedSessions,
  seedWorldBooks,
  toBlocks,
} from './seed';

const STORAGE_KEY = 'loreweave.mock.v1';

interface MockDb {
  identity: UserIdentity;
  characters: Character[];
  worldBooks: WorldBook[];
  sessions: Session[];
  messages: Record<string, Message[]>;
  memories: LongTermMemory[];
  modelSettings: ModelSettings;
  logs: LogEntry[];
  rebuildRequired: boolean;
  /** 每个会话已消耗到第几条预设回复 */
  replyCursor: Record<string, number>;
}

const freshDb = (): MockDb => ({
  identity: structuredClone(seedIdentity),
  characters: structuredClone(seedCharacters),
  worldBooks: structuredClone(seedWorldBooks),
  sessions: structuredClone(seedSessions),
  messages: structuredClone(seedMessages),
  memories: structuredClone(seedMemories),
  modelSettings: structuredClone(seedModelSettings),
  logs: structuredClone(seedLogs),
  rebuildRequired: false,
  replyCursor: {},
});

let db: MockDb = load();

function load(): MockDb {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as MockDb;
  } catch {
    /* 解析失败时回落到种子数据 */
  }
  return freshDb();
}

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(db));
  } catch {
    /* 超配额时静默降级为内存态 */
  }
}

/** 供开发调试：window.__loreweaveResetMock() 清空本地 mock 数据 */
if (typeof window !== 'undefined') {
  (window as unknown as Record<string, unknown>).__loreweaveResetMock = () => {
    db = freshDb();
    save();
    location.reload();
  };
}

/** 模拟网络延迟，让加载态与竞态处理能被真实验证 */
const delay = <T>(value: T, ms = 120 + Math.random() * 80): Promise<T> =>
  new Promise((resolve) => setTimeout(() => resolve(value), ms));

const clone = <T>(v: T): T => structuredClone(v);
const uid = (prefix: string) => `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

const stamp = () => {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${MOCK_TODAY} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

const notFound = (what: string): never => {
  throw new Error(`${what}不存在`);
};

/* ── 日志筛选（与设计稿 renderVals 中的时间/级别过滤一致） ── */

const RANGE_DAYS: Record<string, number> = { all: Infinity, '1d': 0, '7d': 7, '30d': 30 };

function filterLogs(query: LogQuery): LogEntry[] {
  const limit = RANGE_DAYS[query.range ?? 'all'] ?? Infinity;
  const today = new Date(`${MOCK_TODAY}T00:00:00`).getTime();
  return db.logs.filter((l) => {
    if (limit !== Infinity) {
      const age = Math.round((today - new Date(`${l.date}T00:00:00`).getTime()) / 864e5);
      if (age > limit) return false;
    }
    return !query.level || query.level === 'all' || l.level === query.level;
  });
}

const blobOf = (data: unknown) =>
  new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });

/* ── 全量导出 ZIP ─────────────────────────────────────────
   Mock 环境没有后端压缩能力，这里生成无压缩的标准 ZIP，避免仅修改扩展名。
   ZIP 内各 JSON 文件与 API 接口契约中的全量导出结构一致。 */

const CRC32_TABLE = new Uint32Array(256).map((_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit += 1) {
    value = (value & 1) !== 0 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  }
  return value >>> 0;
});

const crc32 = (data: Uint8Array) => {
  let value = 0xffffffff;
  for (const byte of data) value = CRC32_TABLE[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
};

const zipHeader = (size: number) => {
  const bytes = new Uint8Array(new ArrayBuffer(size));
  return { bytes, view: new DataView(bytes.buffer) };
};

const concatBytes = (parts: Uint8Array[]): Uint8Array<ArrayBuffer> => {
  const output = new Uint8Array(
    new ArrayBuffer(parts.reduce((sum, part) => sum + part.length, 0)),
  );
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
};

function zipOf(files: Record<string, unknown>): Blob {
  const encoder = new TextEncoder();
  const localParts: Uint8Array[] = [];
  const centralParts: Uint8Array[] = [];
  let localOffset = 0;

  for (const [name, value] of Object.entries(files)) {
    const nameBytes = encoder.encode(name);
    const data = encoder.encode(JSON.stringify(value, null, 2));
    const checksum = crc32(data);

    const local = zipHeader(30);
    local.view.setUint32(0, 0x04034b50, true);
    local.view.setUint16(4, 20, true);
    local.view.setUint16(6, 0x0800, true);
    local.view.setUint16(8, 0, true);
    local.view.setUint32(14, checksum, true);
    local.view.setUint32(18, data.length, true);
    local.view.setUint32(22, data.length, true);
    local.view.setUint16(26, nameBytes.length, true);
    localParts.push(local.bytes, nameBytes, data);

    const central = zipHeader(46);
    central.view.setUint32(0, 0x02014b50, true);
    central.view.setUint16(4, 20, true);
    central.view.setUint16(6, 20, true);
    central.view.setUint16(8, 0x0800, true);
    central.view.setUint16(10, 0, true);
    central.view.setUint32(16, checksum, true);
    central.view.setUint32(20, data.length, true);
    central.view.setUint32(24, data.length, true);
    central.view.setUint16(28, nameBytes.length, true);
    central.view.setUint32(42, localOffset, true);
    centralParts.push(central.bytes, nameBytes);

    localOffset += local.bytes.length + nameBytes.length + data.length;
  }

  const centralDirectory = concatBytes(centralParts);
  const end = zipHeader(22);
  end.view.setUint32(0, 0x06054b50, true);
  end.view.setUint16(8, Object.keys(files).length, true);
  end.view.setUint16(10, Object.keys(files).length, true);
  end.view.setUint32(12, centralDirectory.length, true);
  end.view.setUint32(16, localOffset, true);

  const archive = concatBytes([...localParts, centralDirectory, end.bytes]);
  return new Blob([archive.buffer], { type: 'application/zip' });
}

export const mockApi: ApiClient = {
  /* ── 身份 ─────────────────────────────────────────────── */
  getIdentity: () => delay(clone(db.identity)),
  updateIdentity: (patch) => {
    db.identity = { ...db.identity, ...patch };
    save();
    return delay(clone(db.identity), 60);
  },

  /* ── 角色卡 ───────────────────────────────────────────── */
  listCharacters: () => delay(clone(db.characters)),
  createCharacter: () => {
    const c: Character = {
      id: uid('c'),
      name: '新角色',
      introduction: '一句话介绍',
      systemPrompt: '',
      dialogueExamples: [{ user: '', assistant: '' }],
    };
    db.characters = [...db.characters, c];
    save();
    return delay(clone(c));
  },
  updateCharacter: (id, patch) => {
    const i = db.characters.findIndex((c) => c.id === id);
    if (i < 0) notFound('角色卡');
    db.characters[i] = { ...db.characters[i], ...patch };
    save();
    return delay(clone(db.characters[i]), 60);
  },
  deleteCharacter: (id) => {
    if (db.characters.length <= 1) throw new Error('至少保留一张角色卡');
    db.characters = db.characters.filter((c) => c.id !== id);
    save();
    return delay(undefined);
  },

  /* ── 世界书 ───────────────────────────────────────────── */
  listWorldBooks: () => delay(clone(db.worldBooks)),
  createWorldBook: () => {
    const b: WorldBook = { id: uid('b'), name: '新世界书', rawContent: '', entries: [] };
    db.worldBooks = [...db.worldBooks, b];
    save();
    return delay(clone(b));
  },
  updateWorldBook: (id, patch) => {
    const i = db.worldBooks.findIndex((b) => b.id === id);
    if (i < 0) notFound('世界书');
    db.worldBooks[i] = { ...db.worldBooks[i], ...patch };
    save();
    return delay(clone(db.worldBooks[i]), 60);
  },
  deleteWorldBook: (id) => {
    if (db.worldBooks.length <= 1) throw new Error('至少保留一本世界书');
    db.worldBooks = db.worldBooks.filter((b) => b.id !== id);
    // 绑定它的会话降级为「未绑定世界书」，后续不再检索或注入任何世界书内容
    db.sessions = db.sessions.map((s) => (s.worldBookId === id ? { ...s, worldBookId: null } : s));
    save();
    return delay(undefined);
  },
  splitWorldBook: (id) => {
    const book = db.worldBooks.find((b) => b.id === id);
    if (!book) notFound('世界书');
    const paragraphs = (book!.rawContent || '')
      .split(/\n+/)
      .map((x) => x.trim())
      .filter((x) => x.length > 4);
    if (!paragraphs.length) throw new Error('请先粘贴世界书原文');
    const drafts: WorldBookDraftEntry[] = paragraphs.slice(0, 6).map((p) => ({
      name: p.slice(0, 6).replace(/[，。：、,.:].*$/, '') || '未命名',
      category: '待分类',
      content: p,
    }));
    return delay(drafts, 700);
  },
  confirmDrafts: (id, drafts) => {
    const book = db.worldBooks.find((b) => b.id === id);
    if (!book) notFound('世界书');
    const created: WorldBookEntry[] = drafts.map((d) => ({
      id: uid('e'),
      worldBookId: id,
      name: d.name,
      content: d.content,
      category: d.category,
      keywords: [d.name],
      resident: false,
      enabled: true,
    }));
    book!.entries = [...book!.entries, ...created];
    save();
    return delay(clone(created), 500);
  },
  createEntry: (bookId) => {
    const book = db.worldBooks.find((b) => b.id === bookId);
    if (!book) notFound('世界书');
    const e: WorldBookEntry = {
      id: uid('e'),
      worldBookId: bookId,
      name: '新条目',
      content: '在这里填写条目内容，保存后将自动生成 Embedding。',
      category: '未分类',
      keywords: ['关键词'],
      resident: false,
      enabled: true,
    };
    book!.entries = [...book!.entries, e];
    save();
    return delay(clone(e));
  },
  updateEntry: (bookId, entryId, patch) => {
    const book = db.worldBooks.find((b) => b.id === bookId);
    if (!book) notFound('世界书');
    const i = book!.entries.findIndex((e) => e.id === entryId);
    if (i < 0) notFound('条目');
    // 内容类改动会让向量过期；仅切换启用/常驻不影响索引
    const touchesIndex =
      patch.name !== undefined || patch.content !== undefined || patch.keywords !== undefined;
    book!.entries[i] = {
      ...book!.entries[i],
      ...patch,
      indexStale: touchesIndex ? true : book!.entries[i].indexStale,
    };
    save();
    return delay(clone(book!.entries[i]), 40);
  },
  deleteEntry: (bookId, entryId) => {
    const book = db.worldBooks.find((b) => b.id === bookId);
    if (!book) notFound('世界书');
    book!.entries = book!.entries.filter((e) => e.id !== entryId);
    save();
    return delay(undefined);
  },
  reembed: (bookId) => {
    const book = db.worldBooks.find((b) => b.id === bookId);
    if (!book) notFound('世界书');
    const count = book!.entries.filter((e) => e.indexStale).length;
    book!.entries = book!.entries.map((e) => ({ ...e, indexStale: false }));
    save();
    return delay({ count }, 600);
  },

  /* ── 会话与消息 ───────────────────────────────────────── */
  listSessions: () => delay(clone(db.sessions)),
  createSession: (payload: CreateSessionPayload) => {
    const character = db.characters.find((c) => c.id === payload.characterId);
    if (!character) notFound('角色卡');
    const s: Session = {
      id: uid('s'),
      title: `新对话 · ${character!.name}`,
      characterId: payload.characterId,
      worldBookId: payload.worldBookId,
      identitySnapshotId: db.identity.id,
      identityName: db.identity.name,
      identityPersonaName: db.identity.personaName,
      characterName: character!.name,
      worldBookName:
        payload.worldBookId === null
          ? null
          : (db.worldBooks.find((book) => book.id === payload.worldBookId)?.name ?? null),
      roundCount: 0,
      consolidatedRound: 0,
      summary: '尚未生成摘要。',
      createdAt: stamp(),
      updatedAt: stamp(),
    };
    // 开场白取角色卡的第一条对话示例，与设计稿 onStartSession 一致
    const opening = character!.dialogueExamples[0]?.assistant || '你来了。';
    db.sessions = [s, ...db.sessions];
    db.messages[s.id] = [
      {
        id: uid('msg'),
        sessionId: s.id,
        role: 'assistant',
        createdAt: stamp(),
        blocks: toBlocks([{ t: 'dialogue', text: opening }]),
      },
    ];
    save();
    return delay(clone(s));
  },
  updateSession: (id, patch) => {
    const i = db.sessions.findIndex((s) => s.id === id);
    if (i < 0) notFound('会话');
    db.sessions[i] = { ...db.sessions[i], ...patch, updatedAt: stamp() };
    save();
    return delay(clone(db.sessions[i]), 40);
  },
  deleteSession: (id) => {
    db.sessions = db.sessions.filter((s) => s.id !== id);
    delete db.messages[id];
    save();
    return delay(undefined);
  },
  listMessages: (sessionId) => delay(clone(db.messages[sessionId] ?? [])),
  deleteTurn: (sessionId, assistantMessageId) => {
    const messages = db.messages[sessionId] ?? [];
    const assistantIndex = messages.findIndex((message) => message.id === assistantMessageId);
    if (assistantIndex < 0) notFound('消息');
    const firstIndex =
      assistantIndex > 0 && messages[assistantIndex - 1].role === 'user'
        ? assistantIndex - 1
        : assistantIndex;
    messages.splice(firstIndex, assistantIndex - firstIndex + 1);
    save();
    return delay(undefined);
  },
  recommendedReply: () => delay({ content: '我们接下来该怎么做？' }),

  /* ── 长期记忆 ─────────────────────────────────────────── */
  listMemories: (query) =>
    delay(
      clone(
        db.memories.filter(
          (m) =>
            (!query?.characterId || m.characterId === query.characterId) &&
            (!query?.type || m.type === query.type) &&
            (!query?.status || m.status === query.status),
        ),
      ),
    ),
  invalidateMemory: (id) => {
    const i = db.memories.findIndex((m) => m.id === id);
    if (i < 0) notFound('记忆');
    db.memories[i] = { ...db.memories[i], status: '已失效' };
    save();
    return delay(clone(db.memories[i]), 60);
  },

  /* ── 模型配置 ─────────────────────────────────────────── */
  getModelSettings: () => delay(clone(db.modelSettings)),
  testConnection: (group, payload) => {
    const result: ConnectionTestResult = payload.baseUrl.trim()
      ? { ok: true, message: `${group === 'main' ? '主 API' : 'Embedding 模型'} 连接测试通过 · 可保存生效` }
      : { ok: false, message: 'Base URL 不能为空' };
    if (!result.ok) throw new Error(result.message);
    return delay(result, 800);
  },
  saveModelSettings: (group: ModelGroup, payload: ModelEndpointPayload) => {
    const prev = db.modelSettings[group];
    // Embedding 端点或模型变更后必须重建索引，重建前禁止新旧向量混用
    const embeddingChanged =
      group === 'embed' && (prev.baseUrl !== payload.baseUrl || prev.model !== payload.model);
    const newKey = payload.apiKey.trim();
    db.modelSettings[group] = {
      baseUrl: payload.baseUrl,
      model: payload.model,
      keySet: !!newKey || prev.keySet,
      keyTail: newKey ? newKey.slice(-4) : prev.keyTail,
    };
    if (embeddingChanged) db.rebuildRequired = true;
    save();
    return delay(clone(db.modelSettings), 300);
  },
  getIndexStatus: () => delay({ rebuildRequired: db.rebuildRequired }),
  rebuildIndex: () => {
    db.rebuildRequired = false;
    save();
    return delay<IndexStatus>({ rebuildRequired: false }, 900);
  },

  /* ── 日志 ─────────────────────────────────────────────── */
  listLogs: (query) => delay(clone(filterLogs(query))),
  deleteLogs: (query) => {
    const doomed = new Set(filterLogs(query).map((l) => l.id));
    db.logs = db.logs.filter((l) => !doomed.has(l.id));
    save();
    return delay({ count: doomed.size }, 200);
  },
  downloadLogs: (query) => delay(blobOf(filterLogs(query)), 200),

  /* ── 导出（只读，不修改任何业务状态） ─────────────────── */
  exportSession: (sessionId) => {
    const session = db.sessions.find((s) => s.id === sessionId);
    if (!session) notFound('会话');
    return delay(blobOf({ session, messages: db.messages[sessionId] ?? [] }), 300);
  },
  exportAll: () => {
    // 导出包不含 API Key、系统日志与可重建的向量索引（PRD §3.7，架构约束 10）
    const exportedAt = new Date().toISOString();
    return delay(
      zipOf({
        'manifest.json': { schemaVersion: 1, exportedAt, appVersion: '0.1.0' },
        'identity.json': db.identity,
        'characters.json': db.characters,
        'worldbooks.json': db.worldBooks,
        'sessions.json': db.sessions,
        'messages.json': db.messages,
        'memories.json': db.memories,
      }),
      600,
    );
  },
};

/* ── 流式回复 ─────────────────────────────────────────────
   复用设计稿的三条预设剧本，按内容块逐字吐出，
   让 ChatPage 真实走一遍 SSE 渲染路径。 */

export async function mockStreamChat(
  sessionId: string,
  text: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const session = db.sessions.find((s) => s.id === sessionId);
  if (!session) {
    onEvent({ type: 'error', message: '会话不存在' });
    return;
  }

  const list = (db.messages[sessionId] ??= []);
  list.push({
    id: uid('msg'),
    sessionId,
    role: 'user',
    createdAt: stamp(),
    blocks: toBlocks([{ t: 'dialogue', text }]),
  });

  const cursor = db.replyCursor[sessionId] ?? 0;
  const reply = seedReplies[cursor % seedReplies.length];
  db.replyCursor[sessionId] = cursor + 1;

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  const aborted = () => signal?.aborted === true;

  // 未绑定世界书的会话不检索、不注入任何世界书内容（架构约束 2）
  if (session.worldBookId && reply.retrieved?.length) {
    await sleep(280);
    if (aborted()) return;
    onEvent({ type: 'retrieval', entries: reply.retrieved });
  }

  for (let i = 0; i < reply.blocks.length; i += 1) {
    if (aborted()) return;
    const block = reply.blocks[i];
    onEvent({ type: 'block_start', sequence: i, blockType: block.t });
    // 每次吐 2 个字符，模拟 token 级流式
    for (let p = 0; p < block.text.length; p += 2) {
      await sleep(24);
      if (aborted()) return;
      onEvent({ type: 'block_delta', sequence: i, text: block.text.slice(p, p + 2) });
    }
    onEvent({ type: 'block_end', sequence: i });
    await sleep(160);
  }

  if (aborted()) return;

  list.push({
    id: uid('msg'),
    sessionId,
    role: 'assistant',
    createdAt: stamp(),
    retrieved: session.worldBookId ? reply.retrieved : undefined,
    blocks: toBlocks(reply.blocks),
  });

  session.roundCount += 1;
  session.updatedAt = stamp();

  // 每累计 10 轮触发一次长期记忆整理（PRD §4.2）
  if (session.roundCount % 10 === 0) {
    const memoText = '已整理长期记忆 · 来自最近 10 轮对话';
    list.push({
      id: uid('msg'),
      sessionId,
      role: 'memo',
      createdAt: stamp(),
      blocks: toBlocks([{ t: 'dialogue', text: memoText }]),
    });
    session.consolidatedRound = session.roundCount;
    onEvent({ type: 'memo', text: memoText });
  }

  save();
  onEvent({ type: 'done', messageId: list[list.length - 1].id, roundCount: session.roundCount });
}
