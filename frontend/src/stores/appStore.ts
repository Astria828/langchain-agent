/**
 * 全局应用状态：当前身份、角色、世界书与会话。
 *
 * 数据一律通过 services/api.ts 的 client 读写，store 只做缓存与派生，
 * 不持有任何硬编码业务数据（示例数据统一在 services/mock/seed.ts）。
 */

import { create } from 'zustand';
import { client } from '@/services/api';
import { streamChat, streamMessageAction } from '@/services/chatStream';
import type {
  Character,
  ChatStreamEvent,
  CreateSessionPayload,
  LogEntry,
  LogLevel,
  LogQuery,
  LogRange,
  LongTermMemory,
  Message,
  MessageAction,
  MessageBlock,
  MemoryStatus,
  MemoryType,
  ModelEndpointConfig,
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

/** 对话版式：设计稿的两种排版，默认沉浸叙事 */
export type ChatStyle = '沉浸叙事' | '经典气泡';

interface AppState {
  /* ── 加载与提示 ─────────────────────────────────────── */
  bootstrapped: boolean;
  toast: string | null;
  flash: (text: string) => void;
  dismissToast: () => void;
  bootstrap: () => Promise<void>;

  /* ── 界面偏好 ───────────────────────────────────────── */
  chatStyle: ChatStyle;
  showRagHints: boolean;

  /* ── 身份 ───────────────────────────────────────────── */
  identity: UserIdentity | null;
  contentLoading: boolean;
  contentError: string | null;
  loadContent: () => Promise<void>;
  saveIdentity: (patch: UpdateUserIdentityPayload) => Promise<boolean>;

  /* ── 角色卡 ─────────────────────────────────────────── */
  characters: Character[];
  addCharacter: () => Promise<string | null>;
  patchCharacter: (id: string, patch: UpdateCharacterPayload) => Promise<boolean>;
  removeCharacter: (id: string) => Promise<boolean>;

  /* ── 世界书 ─────────────────────────────────────────── */
  worldBooks: WorldBook[];
  worldBooksLoading: boolean;
  worldBooksError: string | null;
  loadWorldBooks: () => Promise<void>;
  addWorldBook: () => Promise<string | null>;
  draftWorldBook: (id: string, patch: Partial<Pick<WorldBook, 'name' | 'rawContent'>>) => void;
  patchWorldBook: (id: string, patch: Partial<Pick<WorldBook, 'name' | 'rawContent'>>) => Promise<boolean>;
  removeWorldBook: (id: string) => Promise<boolean>;
  splitRaw: (bookId: string) => Promise<WorldBookDraftEntry[] | null>;
  confirmDrafts: (bookId: string, drafts: WorldBookDraftEntry[]) => Promise<boolean>;
  addEntry: (bookId: string) => Promise<boolean>;
  draftEntry: (bookId: string, entryId: string, patch: Partial<WorldBookEntry>) => void;
  patchEntry: (bookId: string, entryId: string, patch: Partial<WorldBookEntry>) => Promise<boolean>;
  removeEntry: (bookId: string, entryId: string) => Promise<boolean>;
  reembed: (bookId: string) => Promise<boolean>;

  /* ── 会话与消息 ─────────────────────────────────────── */
  sessions: Session[];
  sessionsLoading: boolean;
  sessionsError: string | null;
  currentSessionId: string | null;
  messages: Message[];
  /** 流式生成中的助手消息，未落库 */
  streaming: Message | null;
  streamRetrieved: string[];
  messageActionPending: { messageId: string; action: MessageAction } | null;
  loadSessions: () => Promise<void>;
  selectSession: (id: string) => Promise<void>;
  startSession: (payload: CreateSessionPayload) => Promise<Session | null>;
  renameSession: (id: string, title: string) => Promise<boolean>;
  removeSession: (id: string) => Promise<void>;
  removeTurn: (assistantMessageId: string) => Promise<boolean>;
  runMessageAction: (assistantMessageId: string, action: MessageAction) => Promise<void>;
  recommendReply: () => Promise<string | null>;
  send: (text: string) => Promise<void>;

  /* ── 长期记忆 ───────────────────────────────────────── */
  memories: LongTermMemory[];
  memoriesLoading: boolean;
  memoriesError: string | null;
  loadMemories: (query?: {
    characterId?: string;
    type?: MemoryType;
    status?: MemoryStatus;
  }) => Promise<void>;
  invalidateMemory: (id: string) => Promise<void>;

  /* ── 模型配置 ───────────────────────────────────────── */
  modelSettings: ModelSettings | null;
  modelSettingsLoading: boolean;
  modelSettingsError: string | null;
  rebuildRequired: boolean;
  loadModelSettings: () => Promise<void>;
  testModel: (group: ModelGroup, payload: ModelEndpointPayload) => Promise<boolean>;
  saveModel: (
    group: ModelGroup,
    payload: ModelEndpointPayload,
  ) => Promise<ModelEndpointConfig | null>;
  rebuildIndex: () => Promise<void>;

  /* ── 日志 ───────────────────────────────────────────── */
  logs: LogEntry[];
  logsLoading: boolean;
  logsError: string | null;
  logLevel: LogLevel | 'all';
  logRange: LogRange;
  setLogLevel: (level: LogLevel | 'all') => void;
  setLogRange: (range: LogRange) => void;
  refreshLogs: () => Promise<void>;
  clearLogs: () => Promise<void>;
}

const errText = (err: unknown) => (err instanceof Error ? err.message : '操作失败');

let toastTimer: ReturnType<typeof setTimeout> | undefined;

export const useAppStore = create<AppState>((set, get) => ({
  bootstrapped: false,
  toast: null,
  chatStyle: '沉浸叙事',
  showRagHints: true,
  identity: null,
  contentLoading: false,
  contentError: null,
  characters: [],
  worldBooks: [],
  worldBooksLoading: false,
  worldBooksError: null,
  sessions: [],
  sessionsLoading: false,
  sessionsError: null,
  currentSessionId: null,
  messages: [],
  streaming: null,
  streamRetrieved: [],
  messageActionPending: null,
  memories: [],
  memoriesLoading: false,
  memoriesError: null,
  modelSettings: null,
  modelSettingsLoading: false,
  modelSettingsError: null,
  rebuildRequired: false,
  logs: [],
  logsLoading: false,
  logsError: null,
  logLevel: 'all',
  logRange: 'all',

  /* 复刻设计稿的 flash()：2600ms 后消失，重复调用重置计时 */
  flash: (text) => {
    clearTimeout(toastTimer);
    set({ toast: text });
    toastTimer = setTimeout(() => set({ toast: null }), 2600);
  },
  dismissToast: () => {
    clearTimeout(toastTimer);
    set({ toast: null });
  },

  bootstrap: async () => {
    if (get().bootstrapped) return;
    // 各业务域独立加载，单个接口失败不阻塞其他页面进入可用状态。
    await Promise.all([
      get().loadSessions(),
      get().loadContent(),
      get().loadWorldBooks(),
      get().loadModelSettings(),
      get().loadMemories(),
    ]);
    set({ bootstrapped: true });
  },

  /* ── 身份 ───────────────────────────────────────────── */
  loadContent: async () => {
    set({ contentLoading: true, contentError: null });
    try {
      const [identity, characters] = await Promise.all([
        client.getIdentity(),
        client.listCharacters(),
      ]);
      set({ identity, characters, contentError: null });
    } catch (err) {
      const message = errText(err);
      set({ contentError: message });
      get().flash(message);
    } finally {
      set({ contentLoading: false });
    }
  },
  saveIdentity: async (patch) => {
    try {
      const saved = await client.updateIdentity(patch);
      set({ identity: saved });
      get().flash('身份已保存 · 下一轮对话生效');
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },

  /* ── 角色卡 ─────────────────────────────────────────── */
  addCharacter: async () => {
    try {
      const c = await client.createCharacter();
      set((s) => ({ characters: [c, ...s.characters] }));
      get().flash('新角色卡已创建');
      return c.id;
    } catch (err) {
      get().flash(errText(err));
      return null;
    }
  },
  patchCharacter: async (id, patch) => {
    try {
      const saved = await client.updateCharacter(id, patch);
      set((s) => ({
        characters: s.characters.map((character) =>
          character.id === id ? saved : character,
        ),
      }));
      get().flash('角色卡已保存');
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  removeCharacter: async (id) => {
    try {
      await client.deleteCharacter(id);
      set((s) => ({ characters: s.characters.filter((c) => c.id !== id) }));
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },

  /* ── 世界书 ─────────────────────────────────────────── */
  loadWorldBooks: async () => {
    set({ worldBooksLoading: true, worldBooksError: null });
    try {
      set({ worldBooks: await client.listWorldBooks(), worldBooksError: null });
    } catch (err) {
      const message = errText(err);
      set({ worldBooksError: message });
      get().flash(message);
    } finally {
      set({ worldBooksLoading: false });
    }
  },
  addWorldBook: async () => {
    try {
      const b = await client.createWorldBook();
      set((s) => ({ worldBooks: [...s.worldBooks, b] }));
      return b.id;
    } catch (err) {
      get().flash(errText(err));
      return null;
    }
  },
  draftWorldBook: (id, patch) => {
    set((s) => ({ worldBooks: s.worldBooks.map((b) => (b.id === id ? { ...b, ...patch } : b)) }));
  },
  patchWorldBook: async (id, patch) => {
    try {
      const saved = await client.updateWorldBook(id, patch);
      set((s) => ({ worldBooks: s.worldBooks.map((b) => (b.id === id ? saved : b)) }));
      return true;
    } catch (err) {
      get().flash(errText(err));
      await get().loadWorldBooks();
      return false;
    }
  },
  removeWorldBook: async (id) => {
    try {
      await client.deleteWorldBook(id);
      set((s) => ({ worldBooks: s.worldBooks.filter((b) => b.id !== id) }));
      get().flash('世界书已删除 · 向量集合已清理');
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  splitRaw: async (bookId) => {
    try {
      const drafts = await client.splitWorldBook(bookId);
      get().flash(`LLM 拆分完成 · 生成 ${drafts.length} 个草稿，请预览确认`);
      return drafts;
    } catch (err) {
      // 拆分失败时原文保留，用户可重试或改为手动创建（PRD §3.3）
      get().flash(errText(err));
      return null;
    }
  },
  confirmDrafts: async (bookId, drafts) => {
    try {
      const created = await client.confirmDrafts(bookId, drafts);
      set((s) => ({
        worldBooks: s.worldBooks.map((b) =>
          b.id === bookId ? { ...b, entries: [...b.entries, ...created] } : b,
        ),
      }));
      const staleCount = created.filter((entry) => entry.indexStale).length;
      get().flash(
        staleCount
          ? `已保存 ${created.length} 个条目 · ${staleCount} 个待重新生成 Embedding`
          : `已保存 ${created.length} 个条目 · Embedding 已生成`,
      );
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  addEntry: async (bookId) => {
    try {
      const e = await client.createEntry(bookId);
      set((s) => ({
        worldBooks: s.worldBooks.map((b) => (b.id === bookId ? { ...b, entries: [...b.entries, e] } : b)),
      }));
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  draftEntry: (bookId, entryId, patch) => {
    const touchesIndex =
      patch.name !== undefined ||
      patch.content !== undefined ||
      patch.keywords !== undefined ||
      patch.category !== undefined ||
      patch.resident !== undefined ||
      patch.enabled !== undefined;
    set((s) => ({
      worldBooks: s.worldBooks.map((b) =>
        b.id !== bookId
          ? b
          : {
              ...b,
              entries: b.entries.map((e) =>
                e.id !== entryId ? e : { ...e, ...patch, indexStale: touchesIndex || e.indexStale },
              ),
            },
      ),
    }));
  },
  patchEntry: async (bookId, entryId, patch) => {
    try {
      const saved = await client.updateEntry(bookId, entryId, patch);
      set((s) => ({
        worldBooks: s.worldBooks.map((b) =>
          b.id !== bookId
            ? b
            : { ...b, entries: b.entries.map((e) => (e.id === entryId ? saved : e)) },
        ),
      }));
      return true;
    } catch (err) {
      get().flash(errText(err));
      await get().loadWorldBooks();
      return false;
    }
  },
  removeEntry: async (bookId, entryId) => {
    try {
      await client.deleteEntry(bookId, entryId);
      set((s) => ({
        worldBooks: s.worldBooks.map((b) =>
          b.id === bookId ? { ...b, entries: b.entries.filter((e) => e.id !== entryId) } : b,
        ),
      }));
      get().flash('条目已删除 · 向量索引已更新');
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  reembed: async (bookId) => {
    try {
      const { count } = await client.reembed(bookId);
      await get().loadWorldBooks();
      get().flash(`已重新生成 ${count} 个条目的 Embedding · 索引已同步`);
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },

  /* ── 会话与消息 ─────────────────────────────────────── */
  loadSessions: async () => {
    set({ sessionsLoading: true, sessionsError: null });
    try {
      const sessions = await client.listSessions();
      const currentId = get().currentSessionId;
      const selected = sessions.find((session) => session.id === currentId) ?? sessions[0] ?? null;
      const messages = selected ? await client.listMessages(selected.id) : [];
      set({
        sessions,
        currentSessionId: selected?.id ?? null,
        messages,
        sessionsError: null,
      });
    } catch (err) {
      const message = errText(err);
      set({ sessionsError: message });
      get().flash(message);
    } finally {
      set({ sessionsLoading: false });
    }
  },
  selectSession: async (id) => {
    const session = get().sessions.find((s) => s.id === id);
    if (!session) return;
    set({
      currentSessionId: id,
      messages: [],
      streaming: null,
      streamRetrieved: [],
      messageActionPending: null,
    });
    try {
      const messages = await client.listMessages(id);
      // 载入期间用户可能又切走了，丢弃过期响应
      if (get().currentSessionId !== id) return;
      set({ messages });
      get().flash(`已载入「${session.title}」· 会话上下文已切换`);
    } catch (err) {
      get().flash(errText(err));
    }
  },
  startSession: async (payload) => {
    try {
      const session = await client.createSession(payload);
      const messages = await client.listMessages(session.id);
      const character = get().characters.find((c) => c.id === payload.characterId);
      set((s) => ({
        sessions: [session, ...s.sessions],
        currentSessionId: session.id,
        messages,
        streaming: null,
        streamRetrieved: [],
        messageActionPending: null,
      }));
      get().flash(`新会话已创建 · 已载入「${character?.name ?? ''}」角色快照`);
      return session;
    } catch (err) {
      get().flash(errText(err));
      return null;
    }
  },
  renameSession: async (id, title) => {
    try {
      const saved = await client.updateSession(id, { title });
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === id ? saved : x)) }));
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  removeSession: async (id) => {
    const target = get().sessions.find((s) => s.id === id);
    try {
      await client.deleteSession(id);
      const rest = get().sessions.filter((s) => s.id !== id);
      set({ sessions: rest });
      if (get().currentSessionId === id) {
        const next = rest[0] ?? null;
        set({
          currentSessionId: next?.id ?? null,
          messages: [],
          streaming: null,
          streamRetrieved: [],
          messageActionPending: null,
        });
        if (next) set({ messages: await client.listMessages(next.id) });
      }
      get().flash(`已删除「${target?.title ?? '对话'}」`);
    } catch (err) {
      get().flash(errText(err));
    }
  },

  removeTurn: async (assistantMessageId) => {
    const { currentSessionId, streaming, messageActionPending } = get();
    if (!currentSessionId || streaming || messageActionPending) return false;
    try {
      await client.deleteTurn(currentSessionId, assistantMessageId);
      const [messages, sessions] = await Promise.all([
        client.listMessages(currentSessionId),
        client.listSessions(),
      ]);
      if (get().currentSessionId !== currentSessionId) return true;
      set({ messages, sessions });
      get().flash('已删除最后一轮对话');
      return true;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },

  runMessageAction: async (assistantMessageId, action) => {
    const { currentSessionId, streaming, messageActionPending } = get();
    if (!currentSessionId || streaming || messageActionPending) return;
    const originalMessage = get().messages.find((message) => message.id === assistantMessageId);
    if (!originalMessage) return;
    const baseSequence = action === 'continue' ? originalMessage.blocks.length : 0;
    set((s) => ({
      messageActionPending: { messageId: assistantMessageId, action },
      messages: s.messages.map((message) =>
        message.id === assistantMessageId
          ? { ...message, blocks: action === 'continue' ? message.blocks : [], retrieved: action === 'continue' ? message.retrieved : [] }
          : message,
      ),
    }));
    try {
      await streamMessageAction(currentSessionId, assistantMessageId, action, (event) => {
        if (event.type === 'error') {
          get().flash(event.message);
          return;
        }
        if (event.type === 'retrieval') {
          set((s) => ({
            messages: s.messages.map((message) =>
              message.id === assistantMessageId
                ? {
                    ...message,
                    retrieved:
                      action === 'continue'
                        ? [...new Set([...(message.retrieved ?? []), ...event.entries])]
                        : event.entries,
                  }
                : message,
            ),
          }));
          return;
        }
        if (event.type === 'block_start') {
          const sequence = baseSequence + event.sequence;
          set((s) => ({
            messages: s.messages.map((message) =>
              message.id === assistantMessageId
                ? {
                    ...message,
                    blocks: [
                      ...message.blocks,
                      {
                        id: `action_stream_${assistantMessageId}_${sequence}`,
                        sequence,
                        type: event.blockType,
                        content: '',
                      },
                    ],
                  }
                : message,
            ),
          }));
          return;
        }
        if (event.type === 'block_delta') {
          const sequence = baseSequence + event.sequence;
          set((s) => ({
            messages: s.messages.map((message) =>
              message.id === assistantMessageId
                ? {
                    ...message,
                    blocks: message.blocks.map((block) =>
                      block.sequence === sequence
                        ? { ...block, content: block.content + event.text }
                        : block,
                    ),
                  }
                : message,
            ),
          }));
        }
      });
      const [messages, sessions] = await Promise.all([
        client.listMessages(currentSessionId),
        client.listSessions(),
      ]);
      if (get().currentSessionId !== currentSessionId) return;
      set({ messages, sessions });
    } catch (err) {
      if (get().currentSessionId === currentSessionId) {
        set((s) => ({
          messages: s.messages.map((message) =>
            message.id === assistantMessageId ? originalMessage : message,
          ),
        }));
      }
      get().flash(errText(err));
    } finally {
      if (get().currentSessionId === currentSessionId) {
        set({ messageActionPending: null });
      }
    }
  },

  recommendReply: async () => {
    const { currentSessionId, streaming, messageActionPending } = get();
    if (!currentSessionId || streaming || messageActionPending) return null;
    try {
      return (await client.recommendedReply(currentSessionId)).content;
    } catch (err) {
      get().flash(errText(err));
      return null;
    }
  },

  send: async (text) => {
    const { currentSessionId } = get();
    if (!currentSessionId) {
      get().flash('请先新建一个对话');
      return;
    }
    if (get().streaming || get().messageActionPending) return;

    const now = new Date().toISOString();
    const userMessage: Message = {
      id: `local_${Date.now()}`,
      sessionId: currentSessionId,
      role: 'user',
      createdAt: now,
      blocks: [{ id: `local_${Date.now()}_b`, sequence: 0, type: 'dialogue', content: text }],
    };
    const draft: Message = {
      id: `stream_${Date.now()}`,
      sessionId: currentSessionId,
      role: 'assistant',
      createdAt: now,
      blocks: [],
    };
    set((s) => ({ messages: [...s.messages, userMessage], streaming: draft, streamRetrieved: [] }));

    const upsertBlock = (sequence: number, mutate: (b: MessageBlock) => MessageBlock) =>
      set((s) => {
        if (!s.streaming) return s;
        const blocks = [...s.streaming.blocks];
        const i = blocks.findIndex((b) => b.sequence === sequence);
        if (i >= 0) blocks[i] = mutate(blocks[i]);
        return { streaming: { ...s.streaming, blocks } };
      });

    const onEvent = (event: ChatStreamEvent) => {
      switch (event.type) {
        case 'retrieval':
          set({ streamRetrieved: event.entries });
          break;
        case 'block_start':
          set((s) =>
            s.streaming
              ? {
                  streaming: {
                    ...s.streaming,
                    blocks: [
                      ...s.streaming.blocks,
                      {
                        id: `${s.streaming.id}_${event.sequence}`,
                        sequence: event.sequence,
                        type: event.blockType,
                        content: '',
                      },
                    ],
                  },
                }
              : s,
          );
          break;
        case 'block_delta':
          upsertBlock(event.sequence, (b) => ({ ...b, content: b.content + event.text }));
          break;
        case 'block_end':
        case 'memo':
          break;
        case 'error':
          get().flash(event.message);
          break;
        case 'done':
          break;
      }
    };

    try {
      await streamChat(currentSessionId, text, onEvent);
      // 以服务端落库结果为准重新拉取，保证内容块顺序与刷新后完全一致
      const [messages, freshSessions] = await Promise.all([
        client.listMessages(currentSessionId),
        client.listSessions(),
      ]);
      set({ messages, streaming: null, streamRetrieved: [], sessions: freshSessions });
    } catch (err) {
      set({ streaming: null, streamRetrieved: [] });
      get().flash(errText(err));
    }
  },

  /* ── 长期记忆 ───────────────────────────────────────── */
  loadMemories: async (query) => {
    set({ memories: [], memoriesLoading: true, memoriesError: null });
    try {
      set({ memories: await client.listMemories(query), memoriesError: null });
    } catch (err) {
      const message = errText(err);
      set({ memoriesError: message });
      get().flash(message);
    } finally {
      set({ memoriesLoading: false });
    }
  },
  invalidateMemory: async (id) => {
    try {
      const updated = await client.invalidateMemory(id);
      set((s) => ({ memories: s.memories.map((m) => (m.id === id ? updated : m)) }));
      get().flash('记忆已标记失效，后续检索将忽略');
    } catch (err) {
      get().flash(errText(err));
    }
  },

  /* ── 模型配置 ───────────────────────────────────────── */
  loadModelSettings: async () => {
    set({ modelSettingsLoading: true, modelSettingsError: null });
    try {
      const [modelSettings, indexStatus] = await Promise.all([
        client.getModelSettings(),
        client.getIndexStatus(),
      ]);
      set({
        modelSettings,
        rebuildRequired: indexStatus.rebuildRequired,
        modelSettingsError: null,
      });
    } catch (err) {
      const message = errText(err);
      set({ modelSettingsError: message });
      get().flash(message);
    } finally {
      set({ modelSettingsLoading: false });
    }
  },
  testModel: async (group, payload) => {
    try {
      const result = await client.testConnection(group, payload);
      get().flash(result.message);
      return result.ok;
    } catch (err) {
      get().flash(errText(err));
      return false;
    }
  },
  saveModel: async (group, payload) => {
    const prev = get().modelSettings;
    try {
      const settings = await client.saveModelSettings(group, payload);
      const embeddingChanged =
        group === 'embed' &&
        !!prev &&
        (prev.embed.baseUrl !== payload.baseUrl || prev.embed.model !== payload.model);
      const { rebuildRequired } = await client.getIndexStatus();
      set({ modelSettings: settings, rebuildRequired });
      get().flash(
        embeddingChanged
          ? 'Embedding 配置已保存 · 需重建索引后方可检索'
          : `${group === 'main' ? '主 API' : 'Embedding'} 配置已保存并生效`,
      );
      return settings[group];
    } catch (err) {
      get().flash(errText(err));
      return null;
    }
  },
  rebuildIndex: async () => {
    try {
      const status = await client.rebuildIndex();
      set({ rebuildRequired: status.rebuildRequired });
      get().flash('索引重建完成 · 世界书与长期记忆已使用新向量空间');
    } catch (err) {
      get().flash(errText(err));
    }
  },

  /* ── 日志 ───────────────────────────────────────────── */
  setLogLevel: (level) => {
    set({ logLevel: level });
    void get().refreshLogs();
  },
  setLogRange: (range) => {
    set({ logRange: range });
    void get().refreshLogs();
  },
  refreshLogs: async () => {
    const query: LogQuery = { level: get().logLevel, range: get().logRange };
    set({ logs: [], logsLoading: true, logsError: null });
    try {
      set({ logs: await client.listLogs(query), logsError: null });
    } catch (err) {
      const message = errText(err);
      set({ logsError: message });
      get().flash(message);
    } finally {
      set({ logsLoading: false });
    }
  },
  clearLogs: async () => {
    const query: LogQuery = { level: get().logLevel, range: get().logRange };
    try {
      const { count } = await client.deleteLogs(query);
      await get().refreshLogs();
      if (!count) {
        get().flash('当前筛选条件下没有日志');
        return;
      }
      get().flash(`已删除 ${count} 条日志`);
    } catch (err) {
      get().flash(errText(err));
    }
  },
}));

/* ── 派生选择器 ─────────────────────────────────────────── */

export const selectCurrentSession = (s: AppState): Session | null =>
  s.sessions.find((x) => x.id === s.currentSessionId) ?? null;

export const selectCurrentCharacter = (s: AppState): Character | null => {
  const session = selectCurrentSession(s);
  return session ? s.characters.find((c) => c.id === session.characterId) ?? null : null;
};

export const selectCurrentWorldBook = (s: AppState): WorldBook | null => {
  const session = selectCurrentSession(s);
  return session?.worldBookId ? s.worldBooks.find((b) => b.id === session.worldBookId) ?? null : null;
};

/** 侧边栏「距下次记忆整理还有 N 轮」 */
export const selectRoundsLeft = (s: AppState): number => {
  const session = selectCurrentSession(s);
  return 10 - ((session?.roundCount ?? 0) % 10);
};

/** 输入框下方只展示当前阶段实际注入的基础对话上下文。 */
export const selectContextHint = (s: AppState): string => {
  const session = selectCurrentSession(s);
  const bookPart = session?.worldBookId ? ' · 世界书已绑定（当前不检索）' : '';
  return `上下文：应用规则 · 身份快照 · 角色卡快照 · 最近 20 轮对话${bookPart}`;
};
