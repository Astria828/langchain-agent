import { useEffect, useState } from 'react';

import ConfirmInline from '@/components/ConfirmInline';
import FilterChips from '@/components/FilterChips';
import PageHeader from '@/components/PageHeader';
import { useAppStore } from '@/stores/appStore';
import { MEMORY_TYPES } from '@/types';
import type { MemoryStatus, MemoryType } from '@/types';

/**
 * 按角色查看和失效长期记忆（设计稿第 6934–6970 行）。
 * 记忆按角色隔离，不同角色之间不会混用（架构约束 1、5）。
 */

/** 五类记忆的配色，取自设计稿 typeColors：[前景, 底色, 边框] */
const TYPE_COLORS: Record<MemoryType, [string, string, string]> = {
  用户偏好: ['#9db8d9', 'rgba(120,160,220,.1)', 'rgba(120,160,220,.22)'],
  角色承诺: ['#d9b98f', 'rgba(240,163,94,.1)', 'rgba(240,163,94,.22)'],
  关系变化: ['#d99db8', 'rgba(220,120,170,.1)', 'rgba(220,120,170,.22)'],
  重要剧情: ['#c98a7a', 'rgba(226,112,78,.12)', 'rgba(226,112,78,.25)'],
  长期目标: ['#a8d9b3', 'rgba(143,214,160,.1)', 'rgba(143,214,160,.22)'],
};

const stars = (importance: number) => '★'.repeat(importance) + '☆'.repeat(Math.max(0, 5 - importance));

export default function MemoriesPage() {
  const characters = useAppStore((s) => s.characters);
  const memories = useAppStore((s) => s.memories);
  const memoriesLoading = useAppStore((s) => s.memoriesLoading);
  const memoriesError = useAppStore((s) => s.memoriesError);
  const loadMemories = useAppStore((s) => s.loadMemories);
  const invalidateMemory = useAppStore((s) => s.invalidateMemory);

  const [characterId, setCharacterId] = useState(() => characters[0]?.id ?? '');
  const [type, setType] = useState<'all' | MemoryType>('all');
  const [status, setStatus] = useState<'all' | MemoryStatus>('all');
  const [pendingInvalidate, setPendingInvalidate] = useState<string | null>(null);

  useEffect(() => {
    if (!characters.some((c) => c.id === characterId)) setCharacterId(characters[0]?.id ?? '');
  }, [characters, characterId]);

  useEffect(() => {
    void loadMemories({
      characterId: characterId || undefined,
      type: type === 'all' ? undefined : type,
      status: status === 'all' ? undefined : status,
    });
  }, [characterId, loadMemories, status, type]);

  const shown = memories.filter(
    (m) =>
      m.characterId === characterId &&
      (type === 'all' || m.type === type) &&
      (status === 'all' || m.status === status),
  );

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '44px clamp(24px,5vw,56px)' }}>
      <div style={{ maxWidth: 820, margin: '0 auto' }}>
        <PageHeader
          title="长期记忆"
          subtitle="每累计 10 轮对话自动整理一次。记忆按角色隔离，只作为历史经历参考。"
          actions={
            <FilterChips
              options={characters.map((c) => ({ id: c.id, label: c.name }))}
              value={characterId}
              onChange={setCharacterId}
            />
          }
        />

        <FilterChips
          size="sm"
          style={{ margin: '30px 0 22px' }}
          options={[
            { id: 'all', label: '全部' },
            ...MEMORY_TYPES.map((t) => ({ id: t, label: t })),
          ]}
          value={type}
          onChange={(id) => setType(id as 'all' | MemoryType)}
        />

        <FilterChips
          size="sm"
          style={{ margin: '0 0 22px' }}
          options={[
            { id: 'all', label: '全部状态' },
            { id: '有效', label: '有效' },
            { id: '已失效', label: '已失效' },
          ]}
          value={status}
          onChange={(id) => setStatus(id as 'all' | MemoryStatus)}
        />

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {memoriesLoading && memories.length === 0 && (
            <div style={{ padding: 40, textAlign: 'center', fontSize: 13, color: 'var(--text-dim-4)' }}>
              正在读取长期记忆…
            </div>
          )}
          {memoriesError && (
            <div className="note-warn">
              长期记忆加载失败：{memoriesError}
              <button
                className="btn-ghost"
                onClick={() => void loadMemories({
                  characterId: characterId || undefined,
                  type: type === 'all' ? undefined : type,
                  status: status === 'all' ? undefined : status,
                })}
                disabled={memoriesLoading}
                style={{ marginLeft: 12, fontSize: 12 }}
              >
                重试
              </button>
            </div>
          )}
          {shown.map((m) => {
            const [fg, bg, bd] = TYPE_COLORS[m.type];
            return (
              <div key={m.id}>
                <div
                  className="card"
                  style={{ padding: '20px 24px', display: 'flex', gap: 18, alignItems: 'flex-start' }}
                >
                <span
                  style={{
                    flex: 'none',
                    fontSize: 11,
                    color: fg,
                    padding: '4px 11px',
                    borderRadius: 12,
                    background: bg,
                    border: `1px solid ${bd}`,
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {m.type}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 14, color: '#ecdfc9', lineHeight: 1.8 }}>{m.content}</div>
                  <div
                    style={{
                      display: 'flex',
                      gap: 14,
                      marginTop: 10,
                      fontSize: 11.5,
                      color: 'var(--text-dim-3)',
                      flexWrap: 'wrap',
                    }}
                  >
                    <span>重要性 {stars(m.importance)}</span>
                    <span>{m.createdAt}</span>
                    <span>来源：{m.sourceLabel}</span>
                    <span style={{ color: m.status === '有效' ? 'var(--ok)' : 'var(--text-dim-2)' }}>
                      ● {m.status}
                    </span>
                  </div>
                </div>
                <button
                  className="btn-dim-danger"
                  onClick={() => setPendingInvalidate(m.id)}
                  disabled={m.status === '已失效'}
                  style={{
                    flex: 'none',
                    fontSize: 11.5,
                    padding: '5px 12px',
                    borderRadius: 14,
                    borderColor: 'var(--line-3)',
                    opacity: m.status === '已失效' ? 0.5 : 1,
                  }}
                >
                  标记失效
                </button>
                </div>
                {pendingInvalidate === m.id && (
                  <ConfirmInline
                    text="标记失效后，该记忆将不再参与后续检索。确定继续？"
                    confirmLabel="标记失效"
                    layout="inline"
                    onConfirm={() => {
                      setPendingInvalidate(null);
                      void invalidateMemory(m.id);
                    }}
                    onCancel={() => setPendingInvalidate(null)}
                    style={{ marginTop: 8 }}
                  />
                )}
              </div>
            );
          })}

          {!memoriesLoading && !memoriesError && shown.length === 0 && (
            <div
              style={{
                padding: '40px',
                textAlign: 'center',
                fontSize: 13,
                color: 'var(--text-dim-4)',
              }}
            >
              该角色下暂无此类长期记忆
            </div>
          )}
        </div>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}
