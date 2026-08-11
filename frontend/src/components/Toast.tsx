import { useAppStore } from '@/stores/appStore';

/** 主区底部居中的轻提示，复刻设计稿 toast（ui-reference.html 第 7157 行） */
export default function Toast() {
  const toast = useAppStore((s) => s.toast);
  if (!toast) return null;

  return (
    <div
      role="status"
      style={{
        position: 'absolute',
        bottom: 40,
        left: '50%',
        zIndex: 20,
        animation: 'toastIn .3s ease both',
        display: 'flex',
        gap: 8,
        alignItems: 'center',
        padding: '10px 18px',
        borderRadius: 24,
        background: 'rgba(30,38,29,.95)',
        border: '1px solid rgba(143,214,160,.3)',
        color: 'var(--ok-bright)',
        fontSize: 13,
        boxShadow: '0 8px 30px rgba(0,0,0,.5)',
        whiteSpace: 'nowrap',
      }}
    >
      ✦ {toast}
    </div>
  );
}
