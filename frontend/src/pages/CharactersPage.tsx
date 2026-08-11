import { useEffect, useState } from 'react';

import ConfirmInline from '@/components/ConfirmInline';
import ResizableDivider, { useResizablePanel } from '@/components/ResizableDivider';
import { useAppStore } from '@/stores/appStore';
import type { Character, DialogueExample } from '@/types';

/**
 * 角色卡创建、编辑、删除与切换（设计稿第 6736–6805 行）。
 * 页面使用本地草稿并一次提交真实后端，核心设定始终不经 RAG 检索。
 */

const cloneCharacter = (character: Character): Character => ({
  ...character,
  dialogueExamples: character.dialogueExamples.map((example) => ({ ...example })),
});

export default function CharactersPage() {
  const characters = useAppStore((state) => state.characters);
  const contentLoading = useAppStore((state) => state.contentLoading);
  const contentError = useAppStore((state) => state.contentError);
  const loadContent = useAppStore((state) => state.loadContent);
  const addCharacter = useAppStore((state) => state.addCharacter);
  const patchCharacter = useAppStore((state) => state.patchCharacter);
  const removeCharacter = useAppStore((state) => state.removeCharacter);
  const flash = useAppStore((state) => state.flash);
  const characterPanel = useResizablePanel({
    storageKey: 'loreweave.layout.characterListWidth',
    defaultWidth: 330,
    minWidth: 260,
    maxWidth: 480,
  });

  const [editingId, setEditingId] = useState(() => characters[0]?.id ?? '');
  const [draft, setDraft] = useState<Character | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  // 新建、删除或首次加载后，保证选中的角色卡始终存在。
  useEffect(() => {
    if (!characters.some((character) => character.id === editingId)) {
      setEditingId(characters[0]?.id ?? '');
    }
  }, [characters, editingId]);

  const editing = characters.find((character) => character.id === editingId) ?? null;

  useEffect(() => {
    setDraft(editing ? cloneCharacter(editing) : null);
    setDeleteId(null);
  }, [editing]);

  const validationError = draft && !draft.name.trim() ? '角色名称不能为空' : null;
  const dirty =
    !!draft &&
    !!editing &&
    (draft.name !== editing.name ||
      draft.introduction !== editing.introduction ||
      draft.systemPrompt !== editing.systemPrompt ||
      JSON.stringify(draft.dialogueExamples) !== JSON.stringify(editing.dialogueExamples));
  const shortName = draft?.name.trim().split(/\s+/)[0] || '角色';

  const patchDraft = (patch: Partial<Character>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  };

  const patchExample = (index: number, part: keyof DialogueExample, value: string) => {
    if (!draft) return;
    patchDraft({
      dialogueExamples: draft.dialogueExamples.map((example, exampleIndex) =>
        exampleIndex === index ? { ...example, [part]: value } : example,
      ),
    });
  };

  const handleAdd = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const id = await addCharacter();
      if (id) setEditingId(id);
    } finally {
      setCreating(false);
    }
  };

  const handleSave = async () => {
    if (!draft || !dirty || validationError || saving) return;
    setSaving(true);
    try {
      await patchCharacter(draft.id, {
        name: draft.name.trim(),
        introduction: draft.introduction,
        systemPrompt: draft.systemPrompt,
        dialogueExamples: draft.dialogueExamples,
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    setDeleteId(null);
    const removed = await removeCharacter(id);
    if (removed) flash('角色卡已删除');
  };

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      <div
        style={{
          width: characterPanel.width,
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
            角色卡
          </div>
          <button
            className="btn-primary"
            onClick={() => void handleAdd()}
            disabled={contentLoading || creating || !!contentError}
            style={{ fontSize: 12.5, padding: '8px 15px' }}
          >
            {creating ? '正在创建…' : '＋ 新建'}
          </button>
        </div>

        {contentError && (
          <div className="note-warn" style={{ marginBottom: 16 }}>
            <div>{contentError}</div>
            <button
              className="btn-ghost"
              onClick={() => void loadContent()}
              disabled={contentLoading}
              style={{ marginTop: 10, fontSize: 11.5, padding: '6px 12px' }}
            >
              {contentLoading ? '正在重试…' : '重新连接后端'}
            </button>
          </div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {characters.map((character) => (
            <div
              key={character.id}
              className="card-list"
              onClick={() => setEditingId(character.id)}
              style={{
                padding: 16,
                background:
                  editingId === character.id ? 'rgba(240,163,94,.09)' : 'var(--surface)',
                border: `1px solid ${editingId === character.id ? 'rgba(240,163,94,.4)' : 'var(--line-2)'}`,
                cursor: 'pointer',
              }}
            >
              <div style={{ minWidth: 0, pointerEvents: 'none' }}>
                <div className="serif" style={{ fontSize: 15, fontWeight: 600 }}>
                  {character.name}
                </div>
                <div
                  style={{
                    fontSize: 11.5,
                    color: 'var(--text-dim-2)',
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {character.introduction}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <ResizableDivider
        width={characterPanel.width}
        defaultWidth={330}
        minWidth={260}
        maxWidth={480}
        minRemainingWidth={300}
        label="调整角色卡列表宽度"
        onResize={characterPanel.setWidth}
      />

      <div
        style={{
          flex: 1,
          minWidth: 0,
          overflowY: 'auto',
          padding: '36px clamp(48px, 6vw, 120px) 36px 30px',
        }}
      >
        {!draft ? (
          <div style={{ color: 'var(--text-dim-3)', fontSize: 13 }}>
            {contentLoading ? '正在读取角色卡…' : '还没有角色卡，点击左上「＋ 新建」开始'}
          </div>
        ) : (
          <div style={{ width: '100%', boxSizing: 'border-box' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <div style={{ flex: 1 }}>
                <input
                  className="input-underline"
                  value={draft.name}
                  onChange={(event) => patchDraft({ name: event.target.value })}
                  style={{ fontSize: 24, padding: '6px 2px' }}
                />
                <input
                  className="input-bare"
                  value={draft.introduction}
                  onChange={(event) => patchDraft({ introduction: event.target.value })}
                  style={{ fontSize: 13, padding: '8px 2px' }}
                />
              </div>
              <button
                className="btn-primary"
                onClick={() => void handleSave()}
                disabled={!dirty || !!validationError || saving}
                style={{ fontSize: 12, padding: '7px 14px' }}
              >
                {saving ? '正在保存…' : '保存'}
              </button>
              <button
                className="btn-outline-danger"
                onClick={() => setDeleteId(draft.id)}
                style={{ fontSize: 12, padding: '7px 14px' }}
              >
                删除
              </button>
            </div>

            {deleteId === draft.id && (
              <ConfirmInline
                layout="inline"
                text="删除后无法恢复；已被历史会话引用的角色卡不会被删除。确定删除？"
                onConfirm={() => void handleDelete(draft.id)}
                onCancel={() => setDeleteId(null)}
                style={{ margin: '12px 0' }}
              />
            )}

            {validationError && <div className="note-warn" style={{ marginTop: 12 }}>{validationError}</div>}

            <div className="hint-inline" style={{ margin: '14px 0 26px' }}>
              <span style={{ color: 'var(--accent-deep)' }}>◈</span>
              {dirty ? '修改尚未保存' : '核心设定已保存，每轮固定注入且不经 RAG 检索'}
            </div>

            <div style={{ marginBottom: 22 }}>
              <div className="label-section" style={{ marginBottom: 9 }}>
                角色系统提示词
              </div>
              <textarea
                className="textarea"
                rows={6}
                value={draft.systemPrompt}
                onChange={(event) => patchDraft({ systemPrompt: event.target.value })}
              />
            </div>

            <div style={{ marginBottom: 22 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <div className="label-section">对话示例</div>
                <div style={{ flex: 1 }} />
                <button
                  className="btn-ghost"
                  onClick={() =>
                    patchDraft({
                      dialogueExamples: [
                        ...draft.dialogueExamples,
                        { user: '', assistant: '' },
                      ],
                    })
                  }
                  style={{ fontSize: 11.5, padding: '5px 13px', borderRadius: 14 }}
                >
                  ＋ 添加一轮
                </button>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {draft.dialogueExamples.map((pair, index) => (
                  <div
                    key={index}
                    style={{
                      borderRadius: 12,
                      background: 'var(--surface)',
                      border: '1px solid var(--line-2)',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                        padding: '9px 16px',
                        borderBottom: '1px solid rgba(255,214,170,.07)',
                      }}
                    >
                      <span style={{ fontSize: 11, color: 'var(--text-dim-2)', letterSpacing: '.1em' }}>
                        第 {index + 1} 轮
                      </span>
                      <div style={{ flex: 1 }} />
                      <button
                        className="x-btn"
                        onClick={() =>
                          patchDraft({
                            dialogueExamples: draft.dialogueExamples.filter(
                              (_, exampleIndex) => exampleIndex !== index,
                            ),
                          })
                        }
                        style={{ flex: 'none', fontSize: 12, padding: '2px 4px' }}
                      >
                        ✕
                      </button>
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        gap: 12,
                        alignItems: 'flex-start',
                        padding: '10px 16px 4px',
                      }}
                    >
                      <span
                        style={{
                          flex: 'none',
                          width: 64,
                          fontSize: 11.5,
                          fontWeight: 600,
                          letterSpacing: '.08em',
                          color: 'var(--speaker-user)',
                          marginTop: 10,
                          whiteSpace: 'nowrap',
                        }}
                      >
                        用户
                      </span>
                      <textarea
                        className="textarea-bare"
                        rows={2}
                        value={pair.user}
                        onChange={(event) => patchExample(index, 'user', event.target.value)}
                        placeholder="用户会说什么…"
                        style={{ flex: 1, fontSize: 13, lineHeight: 1.8, padding: '7px 0' }}
                      />
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        gap: 12,
                        alignItems: 'flex-start',
                        padding: '2px 16px 12px',
                        background: 'rgba(240,163,94,.04)',
                      }}
                    >
                      <span
                        style={{
                          flex: 'none',
                          width: 64,
                          fontSize: 11.5,
                          fontWeight: 600,
                          letterSpacing: '.08em',
                          color: 'var(--accent-deep)',
                          marginTop: 12,
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {shortName}
                      </span>
                      <textarea
                        className="textarea-bare"
                        rows={2}
                        value={pair.assistant}
                        onChange={(event) => patchExample(index, 'assistant', event.target.value)}
                        placeholder="角色如何回应…"
                        style={{ flex: 1, fontSize: 13, lineHeight: 1.8, padding: '9px 0' }}
                      />
                    </div>
                  </div>
                ))}
              </div>

              <div className="hint-inline" style={{ marginTop: 10 }}>
                <span style={{ color: 'var(--accent-deep)' }}>◈</span>
                示例对话用于固定角色的语气与格式，按顺序注入提示词，可添加多轮
              </div>
            </div>
            <div style={{ height: 30 }} />
          </div>
        )}
      </div>
    </div>
  );
}
