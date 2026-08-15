import { useEffect, useState } from 'react';

import ConfirmInline from '@/components/ConfirmInline';
import ResizableDivider, { useResizablePanel } from '@/components/ResizableDivider';
import Toggle from '@/components/Toggle';
import { BookIcon, TrashIcon } from '@/components/icons';
import { useAppStore } from '@/stores/appStore';
import type { WorldBookDraftEntry } from '@/types';

/**
 * 世界书原文、拆分草稿和条目管理（设计稿第 6808–6931 行）。
 *
 * 关键规则（PRD §3.3）：
 * - LLM 仅整理与拆分原文，不扩写、不推测、不修改设定；拆分失败保留原文。
 * - 草稿未经用户确认不会写入正式条目，也不会生成向量。
 * - 条目内容修改后标记索引过期，需重新生成 Embedding 才能被语义检索命中。
 */

export default function WorldBooksPage() {
  const worldBooks = useAppStore((s) => s.worldBooks);
  const worldBooksLoading = useAppStore((s) => s.worldBooksLoading);
  const worldBooksError = useAppStore((s) => s.worldBooksError);
  const loadWorldBooks = useAppStore((s) => s.loadWorldBooks);
  const addWorldBook = useAppStore((s) => s.addWorldBook);
  const draftWorldBook = useAppStore((s) => s.draftWorldBook);
  const patchWorldBook = useAppStore((s) => s.patchWorldBook);
  const removeWorldBook = useAppStore((s) => s.removeWorldBook);
  const splitRaw = useAppStore((s) => s.splitRaw);
  const confirmDrafts = useAppStore((s) => s.confirmDrafts);
  const addEntry = useAppStore((s) => s.addEntry);
  const draftEntry = useAppStore((s) => s.draftEntry);
  const patchEntry = useAppStore((s) => s.patchEntry);
  const removeEntry = useAppStore((s) => s.removeEntry);
  const reembed = useAppStore((s) => s.reembed);
  const worldBookPanel = useResizablePanel({
    storageKey: 'loreweave.layout.worldBookListWidth',
    defaultWidth: 260,
    minWidth: 220,
    maxWidth: 420,
  });

  const [selectedId, setSelectedId] = useState(() => worldBooks[0]?.id ?? '');
  const [rawOpen, setRawOpen] = useState(false);
  const [drafts, setDrafts] = useState<WorldBookDraftEntry[]>([]);
  const [splitting, setSplitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [reembedding, setReembedding] = useState(false);
  const [bookToDelete, setBookToDelete] = useState<string | null>(null);
  const [entryToDelete, setEntryToDelete] = useState<string | null>(null);

  useEffect(() => {
    if (!worldBooks.some((b) => b.id === selectedId)) setSelectedId(worldBooks[0]?.id ?? '');
  }, [worldBooks, selectedId]);

  // 切换世界书时清空上一本残留的草稿
  useEffect(() => {
    setDrafts([]);
  }, [selectedId]);

  const book = worldBooks.find((b) => b.id === selectedId) ?? null;
  const staleCount = book?.entries.filter((e) => e.indexStale).length ?? 0;
  const draftsValid =
    drafts.length > 0 &&
    drafts.every(
      (draft) =>
        draft.name.trim() &&
        draft.content.trim() &&
        draft.keywords.some((keyword) => keyword.trim()),
    );

  const runSplit = async () => {
    if (!book) return;
    setSplitting(true);
    try {
      if (!(await patchWorldBook(book.id, { rawContent: book.rawContent }))) return;
      const result = await splitRaw(book.id);
      if (result) setDrafts(result);
    } finally {
      setSplitting(false);
    }
  };

  const confirmCurrentDrafts = async () => {
    if (!book || !draftsValid) return;
    setConfirming(true);
    try {
      if (await confirmDrafts(book.id, drafts)) setDrafts([]);
    } finally {
      setConfirming(false);
    }
  };

  const reembedCurrentBook = async () => {
    if (!book) return;
    setReembedding(true);
    try {
      await reembed(book.id);
    } finally {
      setReembedding(false);
    }
  };

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      {/* ── 左侧：世界书列表 ─────────────────────────────── */}
      <div
        style={{
          width: worldBookPanel.width,
          flex: 'none',
          overflowY: 'auto',
          padding: '36px 24px',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 22,
          }}
        >
          <div className="serif" style={{ fontSize: 21, fontWeight: 600 }}>
            世界书
          </div>
          <button
            className="btn-primary"
            onClick={() => void addWorldBook().then((id) => id && setSelectedId(id))}
            style={{ fontSize: 12.5, padding: '8px 15px' }}
          >
            ＋ 新建
          </button>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {worldBooks.map((b) => (
            <div
              key={b.id}
              className="card-list"
              style={{
                padding: 16,
                background: selectedId === b.id ? 'rgba(240,163,94,.09)' : 'var(--surface)',
                border: `1px solid ${selectedId === b.id ? 'rgba(240,163,94,.4)' : 'var(--line-2)'}`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                <div
                  onClick={() => setSelectedId(b.id)}
                  style={{ flex: 1, minWidth: 0, cursor: 'pointer' }}
                >
                  <div
                    className="serif"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      fontSize: 15,
                      fontWeight: 600,
                      pointerEvents: 'none',
                    }}
                  >
                    <BookIcon size={15} stroke="var(--accent-deep)" style={{ flex: 'none' }} />
                    {b.name}
                  </div>
                  <div
                    style={{
                      fontSize: 11.5,
                      color: 'var(--text-dim-2)',
                      marginTop: 5,
                      pointerEvents: 'none',
                    }}
                  >
                    {b.entries.length} 个条目
                  </div>
                </div>
                <button
                  className="icon-btn-soft"
                  onClick={() => setBookToDelete(b.id)}
                  title="删除这本世界书"
                  style={{ flex: 'none', width: 26, height: 26 }}
                >
                  <TrashIcon size={14} />
                </button>
              </div>

              {bookToDelete === b.id && (
                <ConfirmInline
                  tone="ember"
                  text="删除将移除全部条目与向量；已被历史会话引用的世界书不会被删除。"
                  onConfirm={() => {
                    setBookToDelete(null);
                    void removeWorldBook(b.id);
                  }}
                  onCancel={() => setBookToDelete(null)}
                  style={{ marginTop: 11, padding: '11px 13px' }}
                />
              )}
            </div>
          ))}
        </div>
      </div>

      <ResizableDivider
        width={worldBookPanel.width}
        defaultWidth={260}
        minWidth={220}
        maxWidth={420}
        minRemainingWidth={320}
        label="调整世界书列表宽度"
        onResize={worldBookPanel.setWidth}
      />

      {/* ── 右侧：原文、草稿与条目 ───────────────────────── */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          overflowY: 'auto',
          padding: '36px clamp(48px, 6vw, 120px) 36px 30px',
        }}
      >
        {worldBooksLoading && worldBooks.length === 0 ? (
          <div style={{ color: 'var(--text-dim-3)', fontSize: 13 }}>正在加载世界书…</div>
        ) : worldBooksError && worldBooks.length === 0 ? (
          <div style={{ color: 'var(--err)', fontSize: 13 }}>
            世界书加载失败：{worldBooksError}
            <button
              className="btn-ghost"
              onClick={() => void loadWorldBooks()}
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              重试
            </button>
          </div>
        ) : !book ? (
          <div style={{ color: 'var(--text-dim-3)', fontSize: 13 }}>还没有世界书，点击左上「＋ 新建」开始</div>
        ) : (
          <div style={{ width: '100%', boxSizing: 'border-box' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
                gap: 12,
                marginBottom: 24,
              }}
            >
              <div style={{ flex: 1, minWidth: 200 }}>
                <input
                  className="input-underline"
                  value={book.name}
                  onChange={(e) => draftWorldBook(book.id, { name: e.target.value })}
                  onBlur={(e) => void patchWorldBook(book.id, { name: e.target.value })}
                  style={{ fontSize: 22, padding: '3px 2px' }}
                />
                <div
                  style={{
                    fontSize: 12.5,
                    color: 'var(--text-dim-2)',
                    marginTop: 5,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {book.entries.length} 个条目 · 已建立向量索引
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  className="btn-ghost"
                  onClick={() => setRawOpen((v) => !v)}
                  style={{ fontSize: 12.5, padding: '8px 16px' }}
                >
                  ⌘ 世界书原文
                </button>
                <button
                  className="btn-ghost"
                  onClick={() => void addEntry(book.id)}
                  style={{ fontSize: 12.5, padding: '8px 16px' }}
                >
                  ＋ 手动添加
                </button>
              </div>
            </div>

            {staleCount > 0 && (
              <div
                style={{
                  marginBottom: 18,
                  padding: '13px 18px',
                  borderRadius: 12,
                  background: 'rgba(224,185,106,.07)',
                  border: '1px solid rgba(224,185,106,.25)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 14,
                  flexWrap: 'wrap',
                }}
              >
                <span
                  style={{
                    fontSize: 12.5,
                    color: 'var(--warn)',
                    lineHeight: 1.7,
                    flex: 1,
                    minWidth: 200,
                  }}
                >
                  {staleCount} 个条目已修改，需重新生成 Embedding 后才能被语义检索命中。
                </span>
                <button
                  className="btn-primary"
                  onClick={() => void reembedCurrentBook()}
                  disabled={reembedding}
                  style={{ flex: 'none', fontSize: 12.5, padding: '8px 18px' }}
                >
                  {reembedding ? '正在生成…' : '重新生成 Embedding'}
                </button>
              </div>
            )}

            {rawOpen && (
              <div
                style={{
                  marginBottom: 20,
                  padding: '22px 24px',
                  borderRadius: 14,
                  background: 'rgba(240,163,94,.05)',
                  border: '1px solid rgba(240,163,94,.18)',
                }}
              >
                <div className="serif" style={{ fontSize: 15.5, fontWeight: 600 }}>
                  粘贴世界书原文，自动拆分为条目
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6, lineHeight: 1.7 }}>
                  LLM 仅整理与拆分，不扩写、不推测、不修改你的设定。原文会随世界书一并保存，拆分失败或需要调整时可重新拆分，也可改为手动创建。
                </div>

                {!!book.rawContent.trim() && (
                  <div
                    style={{
                      marginTop: 12,
                      fontSize: 11.5,
                      color: 'var(--ok)',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                    }}
                  >
                    ✦ 原文已保存 · {book.rawContent.replace(/\s/g, '').length}{' '}
                    字，可随时重新拆分或手动补充条目
                  </div>
                )}

                <textarea
                  className="textarea textarea--deep"
                  rows={6}
                  value={book.rawContent}
                  onChange={(e) => draftWorldBook(book.id, { rawContent: e.target.value })}
                  onBlur={(e) => void patchWorldBook(book.id, { rawContent: e.target.value })}
                  placeholder="粘贴完整的世界观设定原文…"
                  style={{ marginTop: 14, fontSize: 13 }}
                />

                {drafts.length === 0 ? (
                  <button
                    className="btn-primary"
                    onClick={() => void runSplit()}
                    disabled={splitting}
                    style={{ marginTop: 14, fontSize: 13, padding: '10px 22px' }}
                  >
                    {splitting ? '拆分中…' : '✧ LLM 拆分为草稿条目'}
                  </button>
                ) : (
                  <div style={{ marginTop: 18 }}>
                    <div className="label-section" style={{ marginBottom: 12 }}>
                      拆分结果 · {drafts.length} 个草稿条目（可编辑后确认）
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                      {drafts.map((d, i) => (
                        <div
                          key={i}
                          style={{
                            padding: '14px 16px',
                            borderRadius: 12,
                            background: 'var(--surface)',
                            border: '1px dashed rgba(240,163,94,.3)',
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            <input
                              className="input-underline"
                              value={d.name}
                              onChange={(e) =>
                                setDrafts((prev) =>
                                  prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)),
                                )
                              }
                              style={{ flex: 'none', width: 140, fontSize: 14, padding: '3px 2px' }}
                            />
                            <div style={{ flex: 1 }} />
                            <button
                              className="x-btn"
                              onClick={() => setDrafts((prev) => prev.filter((_, j) => j !== i))}
                              style={{ flex: 'none', fontSize: 12 }}
                            >
                              ✕
                            </button>
                          </div>
                          <textarea
                            className="textarea-bare"
                            rows={2}
                            value={d.content}
                            onChange={(e) =>
                              setDrafts((prev) =>
                                prev.map((x, j) => (j === i ? { ...x, content: e.target.value } : x)),
                              )
                            }
                            style={{
                              marginTop: 10,
                              color: 'var(--text-quiet)',
                              fontSize: 12.5,
                              lineHeight: 1.8,
                            }}
                          />
                          <div
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 14,
                              marginTop: 10,
                            }}
                          >
                            <input
                              className="input-underline"
                              value={d.keywords.join('、')}
                              onChange={(e) =>
                                setDrafts((prev) =>
                                  prev.map((x, j) =>
                                    j === i
                                      ? {
                                          ...x,
                                          keywords: e.target.value
                                            .split(/[、，,]/)
                                            .map((keyword) => keyword.trim())
                                            .filter(Boolean),
                                        }
                                      : x,
                                  ),
                                )
                              }
                              placeholder="触发关键词，用顿号分隔"
                              style={{ flex: 1, fontSize: 12.5, padding: '5px 2px' }}
                            />
                            <label
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: 6,
                                flex: 'none',
                                fontSize: 12,
                                color: 'var(--text-quiet)',
                              }}
                            >
                              <input
                                type="checkbox"
                                checked={d.resident}
                                onChange={(e) =>
                                  setDrafts((prev) =>
                                    prev.map((x, j) =>
                                      j === i ? { ...x, resident: e.target.checked } : x,
                                    ),
                                  )
                                }
                              />
                              常驻条目
                            </label>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div style={{ display: 'flex', gap: 10, marginTop: 16 }}>
                      <button
                        className="btn-primary"
                        onClick={() => void confirmCurrentDrafts()}
                        disabled={!draftsValid || confirming}
                        style={{ fontSize: 13, padding: '10px 22px' }}
                      >
                        {confirming ? '正在保存…' : '确认保存 · 生成 Embedding'}
                      </button>
                      <button
                        className="btn-ghost"
                        onClick={() => void runSplit()}
                        disabled={splitting}
                        style={{ fontSize: 13, padding: '10px 20px' }}
                      >
                        重新拆分
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            <div>
              {book.entries.map((entry) => (
                <div
                  key={entry.id}
                  className="entry-row"
                  style={{ opacity: entry.enabled ? 1 : 0.45 }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                    <input
                      className="entry-name"
                      value={entry.name}
                      onChange={(e) => draftEntry(book.id, entry.id, { name: e.target.value })}
                      onBlur={(e) => void patchEntry(book.id, entry.id, { name: e.target.value })}
                      placeholder="条目名称"
                    />
                    <button
                      className={`entry-flag${entry.resident ? ' entry-flag--on' : ''}`}
                      onClick={() => void patchEntry(book.id, entry.id, { resident: !entry.resident })}
                      title="切换常驻"
                    >
                      ◈ 常驻
                    </button>
                    {entry.indexStale && <span className="entry-stale">待重建索引</span>}
                    <div style={{ flex: 1 }} />
                    <Toggle
                      checked={entry.enabled}
                      onChange={() => void patchEntry(book.id, entry.id, { enabled: !entry.enabled })}
                      title="启用 / 停用"
                    />
                    <button
                      className="icon-btn-soft"
                      onClick={() => setEntryToDelete(entry.id)}
                      title="删除条目"
                      style={{ flex: 'none', width: 26, height: 26 }}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </div>

                  {entryToDelete === entry.id && (
                    <ConfirmInline
                      layout="inline"
                      text="删除后该条目与其向量将一并移除，确定删除？"
                      onConfirm={() => {
                        setEntryToDelete(null);
                        void removeEntry(book.id, entry.id);
                      }}
                      onCancel={() => setEntryToDelete(null)}
                      style={{ marginTop: 10 }}
                    />
                  )}

                  <textarea
                    className="entry-text"
                    rows={2}
                    value={entry.content}
                    onChange={(e) => draftEntry(book.id, entry.id, { content: e.target.value })}
                    onBlur={(e) => void patchEntry(book.id, entry.id, { content: e.target.value })}
                    placeholder="条目内容…"
                  />

                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
                    <span className="entry-label">关键词</span>
                    <input
                      className="entry-keywords"
                      value={entry.keywords.join(', ')}
                      onChange={(e) =>
                        draftEntry(book.id, entry.id, { keywords: e.target.value.split(/[,，]/) })
                      }
                      onBlur={(e) =>
                        void patchEntry(book.id, entry.id, {
                          keywords: e.target.value
                            .split(/[,，]/)
                            .map((x) => x.trim())
                            .filter(Boolean),
                        })
                      }
                      placeholder="逗号分隔，如：禁区, 失联"
                    />
                  </div>
                </div>
              ))}
            </div>
            <div style={{ height: 30 }} />
          </div>
        )}
      </div>
    </div>
  );
}
