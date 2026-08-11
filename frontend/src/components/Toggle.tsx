/** 世界书条目的启用开关，复刻设计稿 38×21 的滑块 */

interface Props {
  checked: boolean;
  onChange: () => void;
  title?: string;
}

export default function Toggle({ checked, onChange, title }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      title={title}
      onClick={onChange}
      className="switch"
      style={{ background: checked ? 'var(--grad-accent)' : '#3a2d22' }}
    >
      <span className="switch__knob" style={{ left: checked ? 19 : 3 }} />
    </button>
  );
}
