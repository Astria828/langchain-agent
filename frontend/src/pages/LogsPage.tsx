import { useEffect, useMemo, useState } from 'react';

import FilterChips from '@/components/FilterChips';
import PageHeader from '@/components/PageHeader';
import { client } from '@/services/api';
import { useAppStore } from '@/stores/appStore';
import { saveBlob } from '@/utils/file';
import type { LogLevel, LogRange } from '@/types';

/**
 * 四级系统日志查询与下载（设计稿第 7005–7049 行）。
 * 日志不含 API Key、完整 Prompt 或完整对话正文（PRD §3.8）。
 */

const LEVEL_COLORS: Record<LogLevel, string> = {
  DEBUG: 'var(--lv-debug)',
  INFO: 'var(--lv-info)',
  WARNING: 'var(--lv-warning)',
  ERROR: 'var(--lv-error)',
};

const LEVELS: Array<LogLevel | 'all'> = ['all', 'DEBUG', 'INFO', 'WARNING', 'ERROR'];

const RANGES: Array<{ id: LogRange; label: string }> = [
  { id: 'all', label: '全部时间' },
  { id: '1d', label: '今天' },
  { id: '7d', label: '近 7 天' },
  { id: '30d', label: '近 30 天' },
];

export default function LogsPage() {
  const logs = useAppStore((s) => s.logs);
  const logLevel = useAppStore((s) => s.logLevel);
  const logRange = useAppStore((s) => s.logRange);
  const setLogLevel = useAppStore((s) => s.setLogLevel);
  const setLogRange = useAppStore((s) => s.setLogRange);
  const refreshLogs = useAppStore((s) => s.refreshLogs);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const flash = useAppStore((s) => s.flash);

  useEffect(() => {
    void refreshLogs();
  }, [refreshLogs]);

  // 级别胶囊上的计数只受时间范围影响，不受级别筛选影响，因此单独按 range 取一次全量
  const [rangeLogs, setRangeLogs] = useState<Array<{ level: LogLevel }>>([]);
  useEffect(() => {
    let alive = true;
    void client
      .listLogs({ level: 'all', range: logRange })
      .then((rows) => alive && setRangeLogs(rows))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [logRange, logs]);

  const counts = useMemo(() => {
    const byLevel = new Map<LogLevel | 'all', number>();
    byLevel.set('all', rangeLogs.length);
    for (const l of rangeLogs) byLevel.set(l.level, (byLevel.get(l.level) ?? 0) + 1);
    return byLevel;
  }, [rangeLogs]);

  const download = async () => {
    try {
      saveBlob(
        await client.downloadLogs({ level: logLevel, range: logRange }),
        `loreweave-logs-${logRange}-${logLevel}.json`,
      );
      flash(`已下载筛选结果 · ${logs.length} 条日志（不含密钥与对话正文）`);
    } catch {
      flash('日志下载失败');
    }
  };

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '44px clamp(24px,5vw,56px)' }}>
      <div style={{ maxWidth: 900, margin: '0 auto' }}>
        <PageHeader
          title="系统日志"
          subtitle="记录对话流水线、RAG 检索与记忆整理的运行状态，便于排查问题。"
          actions={
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button
                className="btn-ghost"
                onClick={() => void download()}
                style={{ fontSize: 12.5, padding: '8px 16px' }}
              >
                ↓ 下载筛选结果
              </button>
              <button
                className="btn-dim-danger"
                onClick={() => void clearLogs()}
                style={{ fontSize: 12.5, padding: '8px 16px' }}
              >
                删除筛选结果
              </button>
            </div>
          }
        />

        <FilterChips
          size="sm"
          style={{ margin: '28px 0 10px' }}
          options={RANGES}
          value={logRange}
          onChange={(id) => setLogRange(id as LogRange)}
        />

        <FilterChips
          size="sm"
          style={{ margin: '0 0 12px' }}
          options={LEVELS.map((l) => ({
            id: l,
            label: l === 'all' ? '全部' : l,
            dot: l === 'all' ? '#b3a091' : LEVEL_COLORS[l],
            count: counts.get(l) ?? 0,
          }))}
          value={logLevel}
          onChange={(id) => setLogLevel(id as LogLevel | 'all')}
        />

        <div style={{ fontSize: 11.5, color: 'var(--text-dim-4)', marginBottom: 12 }}>
          共 {logs.length} 条 · 滚动文件保存，可按当前筛选下载或删除
        </div>

        <div
          style={{
            borderRadius: 14,
            background: 'var(--surface-deepest)',
            border: '1px solid var(--line-2)',
            padding: '10px 0',
            fontFamily: 'var(--font-mono)',
          }}
        >
          {logs.map((l) => (
            <div key={l.id} className="log-row">
              <span style={{ flex: 'none', fontSize: 11.5, color: 'var(--text-dim-4)' }}>
                {l.date} {l.time}
              </span>
              <span
                style={{
                  flex: 'none',
                  width: 58,
                  fontSize: 11,
                  fontWeight: 700,
                  color: LEVEL_COLORS[l.level],
                  letterSpacing: '.06em',
                }}
              >
                {l.level}
              </span>
              <span style={{ flex: 'none', fontSize: 11.5, color: 'var(--speaker-user)' }}>
                {l.module}
              </span>
              <span
                style={{
                  flex: 'none',
                  fontSize: 11,
                  color: 'var(--text-dim-3)',
                  padding: '2px 8px',
                  borderRadius: 9,
                  background: 'rgba(255,214,170,.05)',
                  border: '1px solid rgba(255,214,170,.08)',
                }}
              >
                {l.requestId}
              </span>
              <span
                style={{
                  flex: 1,
                  minWidth: 220,
                  fontSize: 12.5,
                  color: 'var(--text-soft)',
                  lineHeight: 1.7,
                  fontFamily: 'var(--font-sans)',
                }}
              >
                {l.message}
              </span>
            </div>
          ))}

          {logs.length === 0 && (
            <div
              style={{
                padding: 40,
                textAlign: 'center',
                fontSize: 13,
                color: 'var(--text-dim-4)',
                fontFamily: 'var(--font-sans)',
              }}
            >
              暂无该级别的日志
            </div>
          )}
        </div>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}
