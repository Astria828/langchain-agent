/** 设计稿中出现的内联 SVG，逐路径复刻 */

interface IconProps {
  size?: number;
  stroke?: string;
  strokeWidth?: number;
  style?: React.CSSProperties;
}

const base = (size: number, stroke: string, strokeWidth: number, style?: React.CSSProperties) => ({
  viewBox: '0 0 24 24',
  width: size,
  height: size,
  fill: 'none',
  stroke,
  strokeWidth,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  style,
});

/** 世界书：翻开的书 */
export function BookIcon({ size = 16, stroke = 'currentColor', strokeWidth = 1.7, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, style)}>
      <path d="M12 6c-1.8-1.6-4.2-2-7-2v14c2.8 0 5.2.4 7 2 1.8-1.6 4.2-2 7-2V4c-2.8 0-5.2.4-7 2z" />
      <path d="M12 6v14" />
    </svg>
  );
}

/** 删除：垃圾桶 */
export function TrashIcon({ size = 15, stroke = 'currentColor', strokeWidth = 1.6, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <path d="M4 7h16" />
      <path d="M9.5 7V5.4c0-.5.4-.9.9-.9h3.2c.5 0 .9.4.9.9V7" />
      <path d="M6.5 7l.8 11.2c.05.7.6 1.3 1.3 1.3h6.8c.7 0 1.25-.6 1.3-1.3L17.5 7" />
      <path d="M10.3 10.8v5.4" />
      <path d="M13.7 10.8v5.4" />
    </svg>
  );
}

/** 折叠/展开对话列表 */
export function PanelIcon({ size = 20, stroke = 'currentColor', strokeWidth = 1.6, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M9.5 4.5v15" />
    </svg>
  );
}
