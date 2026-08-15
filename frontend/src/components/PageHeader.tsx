import type { ReactNode } from 'react';

/**
 * 页面标题区。
 * 记忆、日志、数据管理三页共用"左标题+副标题 / 右操作"的两栏结构；
 * 新会话、身份、API 配置三页只用标题与副标题（不传 actions）。
 */

interface Props {
  title: string;
  subtitle: string;
  actions?: ReactNode;
}

export default function PageHeader({ title, subtitle, actions }: Props) {
  if (!actions) {
    return (
      <>
        <div className="serif" style={{ fontSize: 26, fontWeight: 600 }}>
          {title}
        </div>
        <div className="page-sub">{subtitle}</div>
      </>
    );
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 14,
      }}
    >
      <div>
        <div className="serif" style={{ fontSize: 26, fontWeight: 600 }}>
          {title}
        </div>
        <div className="page-sub">{subtitle}</div>
      </div>
      {actions}
    </div>
  );
}
