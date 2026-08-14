import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import ConfirmInline from './ConfirmInline';
import { TrashIcon } from './icons';
import { useAppStore } from '@/stores/appStore';
import type { Session } from '@/types';

/** 会话搜索、分组、选择与删除（设计稿第 6563–6602 行） */

const GROUPS: Array<[label: string, minDays: number, maxDays: number]> = [
  ['今天', 0, 0],
  ['近 7 天', 1, 7],
  ['更早', 8, Number.POSITIVE_INFINITY],
];

const daysAgo = (timestamp: string) => {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const then = new Date(`${timestamp.slice(0, 10)}T00:00:00`).getTime();
  return Math.round((midnight.getTime() - then) / 864e5);
};

interface SessionListProps {
  width: number;
}

export default function SessionList({ width }: SessionListProps) {
  const navigate = useNavigate();
  const sessions = useAppStore((s) => s.sessions);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const sessionsError = useAppStore((s) => s.sessionsError);
  const loadSessions = useAppStore((s) => s.loadSessions);
  const characters = useAppStore((s) => s.characters);
  const currentSessionId = useAppStore((s) => s.currentSessionId);
  const selectSession = useAppStore((s) => s.selectSession);
  const removeSession = useAppStore((s) => s.removeSession);

  const [query, setQuery] = useState('');
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const groups = useMemo(() => {
    const q = query.trim();
    const matched = sessions.filter((s) => !q || `${s.title}${s.summary}`.includes(q));
    const nameOf = (id: string) => characters.find((c) => c.id === id)?.name ?? '未知角色';
    return GROUPS.map(([label, lo, hi]) => ({
      label,
      items: matched
        .filter((s) => {
          const d = daysAgo(s.updatedAt);
          return d >= lo && d <= hi;
        })
        .map((s) => ({
          session: s,
          meta: `${nameOf(s.characterId)} · ${s.roundCount} 轮 · ${s.updatedAt.slice(5, 16)}`,
        })),
    })).filter((g) => g.items.length > 0);
  }, [sessions, characters, query]);

  const empty = groups.length === 0;

  return (
    <aside
      style={{
        flex: 'none',
        width,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        background: 'rgba(28,20,14,.4)',
      }}
    >
      <div
        style={{
          flex: 'none',
          padding: '16px 14px 12px',
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        <button
          className="btn-primary"
          onClick={() => navigate('/session')}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 8,
            padding: 10,
            borderRadius: 12,
            fontSize: 13,
          }}
        >
          ＋ 新建对话
        </button>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索对话…"
          style={{
            width: '100%',
            boxSizing: 'border-box',
            padding: '8px 12px',
            borderRadius: 10,
            border: '1px solid rgba(255,214,170,.13)',
            background: 'rgba(20,14,10,.6)',
            color: 'var(--text)',
            fontSize: 12.5,
            fontFamily: 'inherit',
          }}
        />
      </div>

      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '0 10px 18px',
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
        }}
      >
        {groups.map((group) => (
          <div key={group.label} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <div
              style={{
                fontSize: 10.5,
                letterSpacing: '.18em',
                color: 'var(--text-dim-3)',
                padding: '0 8px 6px',
              }}
            >
              {group.label}
            </div>
            {group.items.map(({ session, meta }) => (
              <SessionRow
                key={session.id}
                session={session}
                meta={meta}
                active={session.id === currentSessionId}
                confirming={pendingDelete === session.id}
                onPick={() => void selectSession(session.id)}
                onAskDelete={() => setPendingDelete(session.id)}
                onCancelDelete={() => setPendingDelete(null)}
                onDelete={() => {
                  setPendingDelete(null);
                  void removeSession(session.id);
                }}
              />
            ))}
          </div>
        ))}

        {empty && (
          <div
            style={{
              padding: '26px 12px',
              textAlign: 'center',
              fontSize: 12.5,
              color: 'var(--text-dim-3)',
            }}
          >
            {sessionsLoading ? '正在读取对话…' : sessionsError ? '对话加载失败' : '没有匹配的对话'}
            {sessionsError && (
              <button
                className="btn-ghost"
                onClick={() => void loadSessions()}
                disabled={sessionsLoading}
                style={{ display: 'block', margin: '12px auto 0', fontSize: 11.5 }}
              >
                重试
              </button>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

interface RowProps {
  session: Session;
  meta: string;
  active: boolean;
  confirming: boolean;
  onPick: () => void;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onDelete: () => void;
}

function SessionRow({
  session,
  meta,
  active,
  confirming,
  onPick,
  onAskDelete,
  onCancelDelete,
  onDelete,
}: RowProps) {
  const ellipsis: React.CSSProperties = {
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    width: '100%',
    pointerEvents: 'none',
  };

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '2px 0' }}>
        <button
          onClick={onPick}
          style={{
            flex: 1,
            minWidth: 0,
            textAlign: 'left',
            padding: '9px 11px',
            borderRadius: 11,
            border: `1px solid ${active ? 'rgba(240,163,94,.3)' : 'transparent'}`,
            background: active ? 'rgba(240,163,94,.13)' : 'transparent',
            cursor: 'pointer',
            fontFamily: 'inherit',
            display: 'flex',
            flexDirection: 'column',
            gap: 3,
          }}
        >
          <div style={{ fontSize: 13, color: 'var(--text-strong)', fontWeight: 500, ...ellipsis }}>
            {session.title}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-dim-2)', ...ellipsis }}>{meta}</div>
        </button>
        <button
          className="icon-btn"
          onClick={onAskDelete}
          title="删除对话"
          style={{ flex: 'none', width: 28, height: 28 }}
        >
          <TrashIcon size={15} />
        </button>
      </div>
      {confirming && (
        <ConfirmInline
          text="删除后该对话记录不可恢复，确定删除？"
          onConfirm={onDelete}
          onCancel={onCancelDelete}
          style={{ margin: '2px 0 6px' }}
        />
      )}
    </>
  );
}
