import { useEffect, useState } from 'react';

import PageHeader from '@/components/PageHeader';
import { useAppStore } from '@/stores/appStore';
import type { UserIdentity } from '@/types';

/**
 * 姓名、名称与用户设定管理（设计稿第 6973–7002 行）。
 * 页面使用本地草稿，用户确认后一次写入真实后端，避免逐键请求乱序。
 */

type IdentityDraft = Pick<UserIdentity, 'name' | 'personaName' | 'bio'>;

const FIELDS: Array<{
  key: keyof Pick<IdentityDraft, 'name' | 'personaName'>;
  label: string;
  hint: string;
  placeholder: string;
}> = [
  { key: 'name', label: '姓名', hint: '角色在对话中对你的称呼', placeholder: '例：临渊' },
  {
    key: 'personaName',
    label: '名称',
    hint: '当前用户身份或人设的名称',
    placeholder: '例：滞留星港的档案修复师',
  },
];

export default function IdentityPage() {
  const identity = useAppStore((state) => state.identity);
  const contentLoading = useAppStore((state) => state.contentLoading);
  const contentError = useAppStore((state) => state.contentError);
  const loadContent = useAppStore((state) => state.loadContent);
  const saveIdentity = useAppStore((state) => state.saveIdentity);

  const [draft, setDraft] = useState<IdentityDraft | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!identity) return;
    setDraft({ name: identity.name, personaName: identity.personaName, bio: identity.bio });
  }, [identity]);

  if (!identity || !draft) {
    return (
      <div style={{ height: '100%', display: 'grid', placeItems: 'center', padding: 40 }}>
        <div style={{ maxWidth: 520, textAlign: 'center' }}>
          <div className={contentError ? 'note-warn' : 'note-ok'}>
            {contentLoading ? '正在读取身份…' : contentError ?? '正在准备身份数据…'}
          </div>
          {contentError && (
            <button
              className="btn-ghost"
              onClick={() => void loadContent()}
              disabled={contentLoading}
              style={{ marginTop: 16, fontSize: 12.5, padding: '9px 18px' }}
            >
              {contentLoading ? '正在重试…' : '重新连接后端'}
            </button>
          )}
        </div>
      </div>
    );
  }

  const validationError = !draft.name.trim()
    ? '姓名不能为空'
    : !draft.personaName.trim()
      ? '名称不能为空'
      : null;
  const dirty =
    draft.name !== identity.name ||
    draft.personaName !== identity.personaName ||
    draft.bio !== identity.bio;

  const handleSave = async () => {
    if (validationError || !dirty || saving) return;
    const normalized = {
      name: draft.name.trim(),
      personaName: draft.personaName.trim(),
      bio: draft.bio,
    };
    setSaving(true);
    try {
      const saved = await saveIdentity(normalized);
      if (saved) setDraft(normalized);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '52px 56px' }}>
      <div style={{ maxWidth: 640, margin: '0 auto' }}>
        <PageHeader
          title="我的身份"
          subtitle="这些信息会在每轮对话中固定注入，帮助角色理解你是谁、你们之间的关系。"
        />

        <div style={{ height: 36 }} />

        {contentError && <div className="note-warn">{contentError}</div>}

        {FIELDS.map((field) => (
          <div key={field.key} style={{ marginTop: 24 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 10,
                marginBottom: 9,
                flexWrap: 'wrap',
              }}
            >
              <span className="label-section">{field.label}</span>
              <span style={{ fontSize: 11.5, color: 'var(--text-dim-3)' }}>{field.hint}</span>
            </div>
            <input
              className="input"
              value={draft[field.key]}
              onChange={(event) =>
                setDraft((current) =>
                  current ? { ...current, [field.key]: event.target.value } : current,
                )
              }
              placeholder={field.placeholder}
            />
          </div>
        ))}

        <div style={{ marginTop: 24 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 10,
              marginBottom: 9,
              flexWrap: 'wrap',
            }}
          >
            <span className="label-section">用户设定</span>
            <span style={{ fontSize: 11.5, color: 'var(--text-dim-3)' }}>
              你的背景、性格与和角色的关系，随身份一并注入
            </span>
          </div>
          <textarea
            className="textarea"
            rows={5}
            value={draft.bio}
            onChange={(event) =>
              setDraft((current) => (current ? { ...current, bio: event.target.value } : current))
            }
            placeholder="例：来自地球的档案修复师，因一次跃迁事故滞留星港。随身带着一台老式胶片相机…"
          />
        </div>

        {validationError && <div className="note-warn" style={{ marginTop: 16 }}>{validationError}</div>}

        <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginTop: 20 }}>
          <button
            className="btn-primary"
            onClick={() => void handleSave()}
            disabled={!dirty || !!validationError || saving}
            style={{ fontSize: 12.5, padding: '9px 20px' }}
          >
            {saving ? '正在保存…' : '保存身份'}
          </button>
          <span style={{ fontSize: 11.5, color: 'var(--text-dim-3)' }}>
            {dirty ? '修改尚未保存' : '当前身份已保存 · 下一轮对话生效'}
          </span>
        </div>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}
