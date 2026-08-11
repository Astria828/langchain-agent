import { useEffect, useState } from 'react';

import FilterChips from '@/components/FilterChips';
import PageHeader from '@/components/PageHeader';
import { client } from '@/services/api';
import { useAppStore } from '@/stores/appStore';
import { saveBlob } from '@/utils/file';
import type { Message } from '@/types';

/**
 * 会话归档、查看与数据导出（设计稿第 7052–7107 行）。
 * 对话记录按「角色 → 会话」归档；展开查看时保留 action / dialogue 分块样式（PRD §3.7）。
 * 导出为只读操作，不修改会话状态、记忆整理进度或向量索引。
 */

export default function DataPage() {
  const characters = useAppStore((s) => s.characters);
  const sessions = useAppStore((s) => s.sessions);
  const memories = useAppStore((s) => s.memories);
  const identity = useAppStore((s) => s.identity);
  const flash = useAppStore((s) => s.flash);

  const [characterId, setCharacterId] = useState(() => characters[0]?.id ?? '');
  const [openId, setOpenId] = useState<string | null>(null);
  const [openMessages, setOpenMessages] = useState<Message[]>([]);

  useEffect(() => {
    if (!characters.some((c) => c.id === characterId)) setCharacterId(characters[0]?.id ?? '');
  }, [characters, characterId]);

  const list = sessions.filter((s) => s.characterId === characterId);
  const character = characters.find((c) => c.id === characterId);

  const stats = [
    { label: `「${character?.name ?? ''}」会话数`, value: String(list.length) },
    { label: '累计对话轮次', value: String(list.reduce((a, s) => a + s.roundCount, 0)) },
    {
      label: '关联长期记忆',
      value: `${memories.filter((m) => m.characterId === characterId).length} 条`,
    },
  ];

  const toggle = async (id: string) => {
    if (openId === id) {
      setOpenId(null);
      setOpenMessages([]);
      return;
    }
    setOpenId(id);
    setOpenMessages([]);
    try {
      const messages = await client.listMessages(id);
      setOpenMessages(messages);
    } catch {
      flash('会话消息载入失败');
    }
  };

  const exportOne = async (id: string, title: string) => {
    try {
      saveBlob(await client.exportSession(id), `${title}.json`);
      flash(`已导出「${title}」· JSON`);
    } catch {
      flash('导出失败');
    }
  };

  const exportAll = async () => {
    try {
      saveBlob(await client.exportAll(), 'loreweave-export.zip');
      flash('已导出全部数据 ZIP · 角色卡、世界书、记忆与对话记录');
    } catch {
      flash('导出失败');
    }
  };

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '44px clamp(24px,5vw,56px)' }}>
      <div style={{ maxWidth: 860, margin: '0 auto' }}>
        <PageHeader
          title="数据管理"
          subtitle="对话记录按角色归档存储。可查看、导出单个会话，或一键导出全部数据。"
          actions={
            <button
              className="btn-primary"
              onClick={() => void exportAll()}
              style={{ fontSize: 12.5, padding: '9px 18px' }}
            >
              ⇩ 导出全部数据
            </button>
          }
        />

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(3,1fr)',
            gap: 14,
            margin: '30px 0',
          }}
        >
          {stats.map((s) => (
            <div key={s.label} className="card" style={{ padding: '18px 22px' }}>
              <div style={{ fontSize: 12, color: 'var(--text-dim-2)' }}>{s.label}</div>
              <div
                className="serif"
                style={{ fontSize: 24, fontWeight: 600, color: 'var(--accent-bright)', marginTop: 6 }}
              >
                {s.value}
              </div>
            </div>
          ))}
        </div>

        <FilterChips
          options={characters.map((c) => ({ id: c.id, label: c.name }))}
          value={characterId}
          onChange={(id) => {
            setCharacterId(id);
            setOpenId(null);
            setOpenMessages([]);
          }}
          style={{ marginBottom: 20 }}
        />

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {list.map((s) => (
            <div key={s.id} className="card" style={{ padding: '20px 24px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <span className="serif" style={{ fontSize: 15.5, fontWeight: 600 }}>
                  {s.title}
                </span>
                <span
                  style={{
                    fontSize: 11,
                    color: 'var(--accent-pale)',
                    padding: '3px 10px',
                    borderRadius: 12,
                    background: 'rgba(240,163,94,.1)',
                    border: '1px solid rgba(240,163,94,.18)',
                  }}
                >
                  {s.roundCount} 轮
                </span>
                <div style={{ flex: 1 }} />
                <span style={{ fontSize: 11.5, color: 'var(--text-dim-3)' }}>{s.updatedAt}</span>
                <button
                  className="btn-ghost"
                  onClick={() => void toggle(s.id)}
                  style={{ fontSize: 11.5, padding: '5px 13px', borderRadius: 14 }}
                >
                  {openId === s.id ? '收起' : '查看'}
                </button>
                <button
                  className="btn-dim"
                  onClick={() => void exportOne(s.id, s.title)}
                  style={{ fontSize: 11.5, padding: '5px 13px', borderRadius: 14 }}
                >
                  导出 JSON
                </button>
              </div>

              <div
                style={{ fontSize: 12.5, color: 'var(--text-mute)', lineHeight: 1.75, marginTop: 9 }}
              >
                {s.summary}
              </div>

              {openId === s.id && (
                <div
                  style={{
                    marginTop: 14,
                    padding: '16px 20px',
                    borderRadius: 12,
                    background: 'var(--surface-deep)',
                    border: '1px solid rgba(255,214,170,.08)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 12,
                  }}
                >
                  {openMessages.length === 0 && (
                    <div style={{ fontSize: 12.5, color: 'var(--text-dim-4)' }}>载入中…</div>
                  )}
                  {openMessages.map((m) => (
                    <div key={m.id} style={{ display: 'flex', gap: 12, fontSize: 12.5, lineHeight: 1.75 }}>
                      <span
                        style={{
                          flex: 'none',
                          width: 56,
                          color: m.role === 'user' ? 'var(--speaker-user)' : 'var(--speaker-char)',
                          fontWeight: 600,
                        }}
                      >
                        {m.role === 'user' ? identity?.name : m.role === 'memo' ? '记忆' : character?.name}
                      </span>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {m.blocks.map((b) => (
                          <div
                            key={b.id}
                            style={{
                              // 归档视图沿用 action 斜体 / dialogue 正常的分块样式
                              color: b.type === 'action' ? 'var(--speaker-user)' : 'var(--text-quiet)',
                              fontStyle: b.type === 'action' ? 'italic' : 'normal',
                            }}
                          >
                            {b.content}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}

          {list.length === 0 && (
            <div
              style={{
                padding: 40,
                textAlign: 'center',
                fontSize: 13,
                color: 'var(--text-dim-4)',
              }}
            >
              该角色下还没有会话记录
            </div>
          )}
        </div>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}
