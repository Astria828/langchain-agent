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

/** 重说：回环箭头 */
export function RegenerateIcon({ size = 13, stroke = 'currentColor', strokeWidth = 1.5, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <path d="M20 12a8 8 0 1 1-2.34-5.66L20 8.6" />
      <path d="M20 3.8v4.8h-4.8" />
    </svg>
  );
}

/** 继续说：细长右箭头 */
export function ContinueIcon({ size = 13, stroke = 'currentColor', strokeWidth = 1.5, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <path d="M4 12h15" />
      <path d="M13.4 6.6L18.8 12l-5.4 5.4" />
    </svg>
  );
}

/**
 * 发送：纸飞机。
 * 机身以 45° 对角线左右对称（尾点与底点关于对角线互为镜像），
 * 折线顶点落在「机头 → 尾底中点」的连线上，收在 83% 处形成尖窄的尾凹。
 * 转角圆滑交给 strokeLinejoin: round，不额外画圆弧。
 */
export function SendIcon({ size = 19, stroke = 'currentColor', strokeWidth = 1.8, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <path d="M21 3L3.2 10.3l7.4 3.1 3.1 7.4z" />
      <path d="M21 3l-10.4 10.4" />
    </svg>
  );
}

/** 推荐回复：四角星芒 */
export function SparkIcon({ size = 14, stroke = 'currentColor', strokeWidth = 1.5, style }: IconProps) {
  return (
    <svg {...base(size, stroke, strokeWidth, { pointerEvents: 'none', ...style })}>
      <path d="M11 3.4l1.75 4.85L17.6 10l-4.85 1.75L11 16.6l-1.75-4.85L4.4 10l4.85-1.75z" />
      <path d="M17.8 14.6l.75 2.05 2.05.75-2.05.75-.75 2.05-.75-2.05-2.05-.75 2.05-.75z" />
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
