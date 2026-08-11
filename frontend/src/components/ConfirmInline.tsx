/**
 * 内联删除确认框。
 * 设计稿在对话列表、世界书列表、世界书条目三处重复了同一模式，
 * 差异只有配色（危险红 vs 琥珀红）与文案。
 */

interface Props {
  text: string;
  tone?: 'danger' | 'ember';
  /** row：确认与取消并排铺满；inline：靠右两个小胶囊 */
  layout?: 'row' | 'inline';
  onConfirm: () => void;
  onCancel: () => void;
  style?: React.CSSProperties;
}

const TONES = {
  danger: { bg: 'rgba(217,147,131,.08)', border: 'rgba(217,147,131,.25)' },
  ember: { bg: 'rgba(226,112,78,.08)', border: 'rgba(226,112,78,.24)' },
};

export default function ConfirmInline({
  text,
  tone = 'danger',
  layout = 'row',
  onConfirm,
  onCancel,
  style,
}: Props) {
  const t = TONES[tone];

  if (layout === 'inline') {
    return (
      <div
        style={{
          padding: '11px 14px',
          borderRadius: 11,
          background: 'rgba(226,112,78,.07)',
          border: '1px solid rgba(226,112,78,.22)',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          ...style,
        }}
      >
        <span style={{ fontSize: 12, color: '#d9b0a4', flex: 1, minWidth: 180, lineHeight: 1.6 }}>
          {text}
        </span>
        <button
          className="btn-danger"
          onClick={onConfirm}
          style={{ flex: 'none', fontSize: 12, padding: '6px 16px', borderRadius: 16 }}
        >
          删除
        </button>
        <button
          className="btn-cancel"
          onClick={onCancel}
          style={{ flex: 'none', fontSize: 12, padding: '6px 16px', borderRadius: 16 }}
        >
          取消
        </button>
      </div>
    );
  }

  return (
    <div
      style={{
        padding: '10px 12px',
        borderRadius: 11,
        background: t.bg,
        border: `1px solid ${t.border}`,
        ...style,
      }}
    >
      <div style={{ fontSize: 11.5, color: '#d9b0a4', lineHeight: 1.6 }}>{text}</div>
      <div style={{ display: 'flex', gap: 8, marginTop: 9 }}>
        <button className="btn-danger" onClick={onConfirm} style={{ flex: 1, fontSize: 12, padding: '6px 0' }}>
          删除
        </button>
        <button className="btn-cancel" onClick={onCancel} style={{ flex: 1, fontSize: 12, padding: '6px 0' }}>
          取消
        </button>
      </div>
    </div>
  );
}
