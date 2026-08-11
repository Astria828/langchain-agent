/**
 * 筛选胶囊组。
 * 记忆页（角色 / 类型）、日志页（时间范围 / 级别）、数据管理页（角色）共用。
 */

export interface ChipOption {
  id: string;
  label: string;
  /** 日志级别左侧的彩色圆点 */
  dot?: string;
  /** 日志级别右侧的条数 */
  count?: number;
}

interface Props {
  options: ChipOption[];
  value: string;
  onChange: (id: string) => void;
  /** md：角色切换（12.5px / 8px 16px）；sm：类型与级别（12px / 7px 15px） */
  size?: 'md' | 'sm';
  style?: React.CSSProperties;
}

export default function FilterChips({ options, value, onChange, size = 'md', style }: Props) {
  const dims =
    size === 'md'
      ? { fontSize: 12.5, padding: '8px 16px' }
      : { fontSize: 12, padding: '7px 15px' };

  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', ...style }}>
      {options.map((o) => (
        <button
          key={o.id}
          className={`chip${size === 'sm' ? ' chip--sm' : ''}${value === o.id ? ' chip--on' : ''}`}
          onClick={() => onChange(o.id)}
          style={{
            ...dims,
            ...(o.dot !== undefined
              ? { display: 'flex', alignItems: 'center', gap: 7 }
              : null),
          }}
        >
          {o.dot !== undefined && (
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: o.dot,
                pointerEvents: 'none',
              }}
            />
          )}
          {o.label}
          {o.count !== undefined && (
            <span style={{ color: 'var(--text-dim-3)', pointerEvents: 'none' }}>{o.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
