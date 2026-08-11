import { NavLink } from 'react-router-dom';
import { useAppStore, selectRoundsLeft } from '@/stores/appStore';
import { BookIcon } from './icons';

/** 设计稿的分组导航栏（ui-reference.html 第 6524–6557 行） */

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const NAV_GROUPS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: '设定',
    items: [
      { to: '/characters', label: '角色卡', icon: '◈' },
      { to: '/worldbooks', label: '世界书', icon: '' },
      { to: '/identity', label: '我的身份', icon: '☾' },
    ],
  },
  {
    label: '演绎',
    items: [
      { to: '/chat', label: '对话', icon: '✎' },
      { to: '/session', label: '新会话', icon: '✧' },
      { to: '/memories', label: '记忆', icon: '✦' },
    ],
  },
  {
    label: '系统',
    items: [
      // 路径避开 /api*：那是 vite.config.ts 里转发到后端的代理前缀
      { to: '/settings', label: 'API 配置', icon: '⚙' },
      { to: '/data', label: '数据管理', icon: '⛁' },
      { to: '/logs', label: '系统日志', icon: '≣' },
    ],
  },
];

interface Props {
  width: number;
}

export default function Sidebar({ width }: Props) {
  const roundsLeft = useAppStore(selectRoundsLeft);

  return (
    <nav
      style={{
        width,
        flex: 'none',
        display: 'flex',
        flexDirection: 'column',
        padding: '20px 16px 16px',
        gap: 4,
        background: 'rgba(22,15,9,.6)',
        overflowY: 'auto',
        minHeight: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '2px 10px 18px' }}>
        <div style={{ width: 40, height: 40, flex: 'none', display: 'grid', placeItems: 'center' }}>
          <img src="/logo.png" alt="LoreWeave" style={{ width: 40, height: 40, objectFit: 'contain' }} />
        </div>
        <div>
          <div className="serif" style={{ fontWeight: 600, fontSize: 17, letterSpacing: '.06em' }}>
            织境
          </div>
          <div
            style={{
              fontSize: 10.5,
              color: 'var(--text-dim-2)',
              letterSpacing: '.14em',
              marginTop: 1,
            }}
          >
            LOREWEAVE
          </div>
        </div>
      </div>

      {NAV_GROUPS.map((group, gi) => (
        <div
          key={group.label}
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
            paddingBottom: 14,
            marginBottom: 12,
            borderBottom: `1px solid ${
              gi === NAV_GROUPS.length - 1 ? 'transparent' : 'rgba(255,214,170,.07)'
            }`,
          }}
        >
          <div
            style={{
              fontSize: 10,
              letterSpacing: '.2em',
              color: 'var(--text-dim-4)',
              padding: '0 12px 6px',
            }}
          >
            {group.label}
          </div>
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className="nav-btn"
              style={({ isActive }) => ({
                border: `1px solid ${isActive ? 'rgba(240,163,94,.3)' : 'transparent'}`,
                background: isActive ? 'rgba(240,163,94,.13)' : 'transparent',
                color: isActive ? 'var(--accent-bright)' : '#b3a091',
              })}
            >
              <span
                style={{
                  fontSize: 16,
                  width: 20,
                  display: 'grid',
                  placeItems: 'center',
                  pointerEvents: 'none',
                }}
              >
                {item.icon || <BookIcon size={16} />}
              </span>
              <span style={{ pointerEvents: 'none' }}>{item.label}</span>
            </NavLink>
          ))}
        </div>
      ))}

      <div style={{ flex: 1, minHeight: 14 }} />

      <div
        style={{
          flex: 'none',
          padding: 12,
          borderRadius: 12,
          background: 'rgba(240,163,94,.06)',
          border: '1px solid rgba(240,163,94,.14)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 12,
            color: 'var(--accent-soft)',
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              background: 'var(--ok)',
              boxShadow: '0 0 8px rgba(143,214,160,.8)',
              animation: 'glowPulse 3s infinite',
            }}
          />
          记忆引擎运行中
        </div>
        <div
          style={{ fontSize: 11, color: 'var(--text-dim-2)', marginTop: 5, lineHeight: 1.5 }}
        >
          距下次记忆整理还有 {roundsLeft} 轮对话
        </div>
      </div>
    </nav>
  );
}
