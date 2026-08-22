import { useEffect, useState } from 'react';

import PageHeader from '@/components/PageHeader';
import { PencilIcon, SearchIcon } from '@/components/icons';
import { useAppStore } from '@/stores/appStore';
import type { ModelEndpointConfig, ModelGroup } from '@/types';

/**
 * 主 API、Embedding API 与索引重建（设计稿第 7110–7154 行）。
 *
 * 关键规则（PRD §3.6）：
 * - 只有连接测试通过的配置才允许保存生效。
 * - API Key 不明文回显，前端只显示「已配置」状态与脱敏尾号。
 * - Embedding 模型或向量维度变更后必须重建索引，重建完成前不混用新旧向量。
 */

interface GroupDef {
  id: ModelGroup;
  Icon: typeof PencilIcon;
  title: string;
  desc: string;
}

/**
 * 应用自己组装的请求字段。与后端 RESERVED_EXTRA_BODY_KEYS 保持一致：
 * 覆盖这些字段会直接破坏内容块协议，在提交前就挡住比等 422 更好懂。
 */
const RESERVED_EXTRA_BODY_KEYS = [
  'model',
  'messages',
  'stream',
  'response_format',
  'max_tokens',
  'temperature',
];

/** 返回额外请求参数的问题描述；无问题返回 null */
function extraBodyProblem(raw: string): string | null {
  const text = raw.trim();
  if (!text) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return '不是合法 JSON，请检查括号、引号与逗号';
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return '必须是一个 JSON 对象，例如 {"provider":{…}}';
  }
  const conflicts = RESERVED_EXTRA_BODY_KEYS.filter((key) => key in parsed);
  if (conflicts.length) {
    return `不能覆盖应用自身设置的字段：${conflicts.join('、')}`;
  }
  return null;
}

const GROUPS: GroupDef[] = [
  { id: 'main', Icon: PencilIcon, title: '主 API', desc: '对话生成、世界书拆分、记忆提取与整合' },
  {
    id: 'embed',
    Icon: SearchIcon,
    title: 'Embedding 模型',
    desc: '世界书条目与长期记忆的向量化，供 RAG 语义检索',
  },
];

/** 与记忆 / 数据 / 日志三页一致的居中版心，不再单独收窄到 640 */
const PAGE_MAX_WIDTH = 900;

interface Draft {
  baseUrl: string;
  model: string;
  apiKey: string;
  extraBody: string;
}

type BusyAction = `${ModelGroup}:test` | `${ModelGroup}:save` | 'embed:rebuild';

export default function ApiSettingsPage() {
  const modelSettings = useAppStore((s) => s.modelSettings);
  const modelSettingsLoading = useAppStore((s) => s.modelSettingsLoading);
  const modelSettingsError = useAppStore((s) => s.modelSettingsError);
  const rebuildRequired = useAppStore((s) => s.rebuildRequired);
  const loadModelSettings = useAppStore((s) => s.loadModelSettings);
  const testModel = useAppStore((s) => s.testModel);
  const saveModel = useAppStore((s) => s.saveModel);
  const rebuildIndex = useAppStore((s) => s.rebuildIndex);

  const [drafts, setDrafts] = useState<Record<ModelGroup, Draft> | null>(null);
  const [tested, setTested] = useState<Record<ModelGroup, boolean>>({ main: false, embed: false });
  const [busyAction, setBusyAction] = useState<BusyAction | null>(null);

  // 从服务端的脱敏配置初始化草稿；apiKey 始终留空，永不回显已保存的密钥
  useEffect(() => {
    if (!modelSettings || drafts) return;
    setDrafts({
      main: {
        baseUrl: modelSettings.main.baseUrl,
        model: modelSettings.main.model,
        apiKey: '',
        extraBody: modelSettings.main.extraBody,
      },
      embed: {
        baseUrl: modelSettings.embed.baseUrl,
        model: modelSettings.embed.model,
        apiKey: '',
        extraBody: '',
      },
    });
  }, [modelSettings, drafts]);

  if (!modelSettings || !drafts) {
    return (
      <div style={{ height: '100%', overflowY: 'auto', padding: '44px clamp(24px,5vw,56px)' }}>
        <div style={{ maxWidth: PAGE_MAX_WIDTH, margin: '0 auto' }}>
          <PageHeader
            title="API 配置"
            subtitle="主 API 负责对话生成等常规功能；Embedding 模型用于世界书与记忆的向量检索。"
          />
          <div style={{ marginTop: 30 }}>
            <div className={modelSettingsError ? 'note-warn' : 'note-ok'}>
              {modelSettingsLoading
                ? '正在读取后端模型配置…'
                : modelSettingsError ?? '正在准备模型配置…'}
            </div>
            {modelSettingsError && (
              <button
                className="btn-ghost"
                onClick={() => void loadModelSettings()}
                disabled={modelSettingsLoading}
                style={{ marginTop: 16, fontSize: 12.5, padding: '9px 18px' }}
              >
                {modelSettingsLoading ? '正在重试…' : '重新连接后端'}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  const patch = (group: ModelGroup, field: keyof Draft, value: string) => {
    setDrafts((prev) => (prev ? { ...prev, [group]: { ...prev[group], [field]: value } } : prev));
    // 任何字段改动都会作废上一次的连接测试结果
    setTested((prev) => ({ ...prev, [group]: false }));
  };

  const handleTest = async (group: ModelGroup) => {
    setBusyAction(`${group}:test`);
    try {
      const ok = await testModel(group, drafts[group]);
      setTested((prev) => ({ ...prev, [group]: ok }));
    } finally {
      setBusyAction(null);
    }
  };

  const handleSave = async (group: ModelGroup) => {
    setBusyAction(`${group}:save`);
    try {
      const saved = await saveModel(group, drafts[group]);
      if (!saved) return;
      setDrafts((prev) =>
        prev
          ? {
              ...prev,
              [group]: {
                baseUrl: saved.baseUrl,
                model: saved.model,
                apiKey: '',
                extraBody: saved.extraBody,
              },
            }
          : prev,
      );
      setTested((prev) => ({ ...prev, [group]: false }));
    } finally {
      setBusyAction(null);
    }
  };

  const handleRebuild = async () => {
    setBusyAction('embed:rebuild');
    try {
      await rebuildIndex();
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '44px clamp(24px,5vw,56px)' }}>
      <div style={{ maxWidth: PAGE_MAX_WIDTH, margin: '0 auto' }}>
        <PageHeader
          title="API 配置"
          subtitle="主 API 负责对话生成等常规功能；Embedding 模型用于世界书与记忆的向量检索。"
        />

        <div style={{ height: 12 }} />

        {GROUPS.map((g) => (
          <GroupCard
            key={g.id}
            def={g}
            saved={modelSettings[g.id]}
            draft={drafts[g.id]}
            tested={tested[g.id]}
            showRebuild={g.id === 'embed' && rebuildRequired}
            onField={(field, value) => patch(g.id, field, value)}
            testing={busyAction === `${g.id}:test`}
            saving={busyAction === `${g.id}:save`}
            rebuilding={busyAction === 'embed:rebuild'}
            actionLocked={busyAction !== null}
            onTest={() => void handleTest(g.id)}
            onSave={() => void handleSave(g.id)}
            onRebuild={() => void handleRebuild()}
          />
        ))}

        {/* 去掉描边与底色，降为分隔线上方的一行说明，文字色沿用 note-ok */}
        <div
          style={{
            borderTop: '1px solid rgba(255,214,170,.07)',
            paddingTop: 18,
            fontSize: 12,
            color: '#9dbfa5',
            lineHeight: 1.7,
          }}
        >
          ✦ API Key 加密保存在本地，不会明文回传或写入日志 · 仅连接测试通过后可保存生效
        </div>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}

interface CardProps {
  def: GroupDef;
  saved: ModelEndpointConfig;
  draft: Draft;
  tested: boolean;
  showRebuild: boolean;
  testing: boolean;
  saving: boolean;
  rebuilding: boolean;
  actionLocked: boolean;
  onField: (field: keyof Draft, value: string) => void;
  onTest: () => void;
  onSave: () => void;
  onRebuild: () => void;
}

function GroupCard({
  def,
  saved,
  draft,
  tested,
  showRebuild,
  testing,
  saving,
  rebuilding,
  actionLocked,
  onField,
  onTest,
  onSave,
  onRebuild,
}: CardProps) {
  const newKey = draft.apiKey.trim();
  // 额外参数只对主模型开放，Embedding 的草稿恒为空串，不参与比较
  const supportsExtraBody = def.id === 'main';
  const extraBodyError = supportsExtraBody ? extraBodyProblem(draft.extraBody) : null;
  const dirty =
    saved.baseUrl !== draft.baseUrl ||
    saved.model !== draft.model ||
    (supportsExtraBody && saved.extraBody !== draft.extraBody.trim()) ||
    !!newKey ||
    !saved.keySet;
  const status = dirty ? (tested ? '测试通过 · 待保存' : '未验证') : '已生效';
  const green = status !== '未验证';
  const canSave = dirty && tested;

  const fields: Array<{
    key: keyof Draft;
    label: string;
    type: string;
    placeholder: string;
    masked: boolean;
  }> = [
    { key: 'baseUrl', label: 'BASE URL', type: 'text', placeholder: 'https://…', masked: false },
    {
      key: 'apiKey',
      label: 'API KEY',
      type: 'password',
      placeholder: saved.keySet ? '已加密保存 · 留空表示不修改' : 'sk-…',
      masked: saved.keySet,
    },
    { key: 'model', label: '模型名称', type: 'text', placeholder: '模型 ID', masked: false },
  ];

  const { Icon } = def;

  return (
    <div className="settings-group">
      {/* 左栏：分组身份与状态 */}
      <div>
        <div
          className="serif"
          style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 15.5, fontWeight: 600 }}
        >
          <Icon stroke="var(--accent-deep)" style={{ flex: 'none' }} />
          {def.title}
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-dim-2)', marginTop: 8, lineHeight: 1.7 }}>
          {def.desc}
        </div>
        <div className="settings-status" style={{ color: green ? 'var(--ok)' : 'var(--warn)' }}>
          {status}
        </div>
      </div>

      {/* 右栏：字段与操作 */}
      <div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {fields.map((f) => (
            <div key={f.key}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <span className="label-section">{f.label}</span>
                {f.masked && (
                  <span style={{ fontSize: 11, color: 'var(--ok)', whiteSpace: 'nowrap' }}>
                    已配置 · ••••{saved.keyTail}
                  </span>
                )}
              </div>
              <input
                className="input input--deep input--flat"
                type={f.type}
                value={draft[f.key]}
                onChange={(e) => onField(f.key, e.target.value)}
                placeholder={f.placeholder}
                style={{ padding: '12px 16px', lineHeight: 'normal' }}
              />
            </div>
          ))}

          {supportsExtraBody && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <span className="label-section">额外请求参数</span>
                <span style={{ fontSize: 11, color: 'var(--text-dim-2)' }}>可选 · JSON 对象</span>
              </div>
              <textarea
                className="input input--deep input--flat"
                value={draft.extraBody}
                onChange={(e) => onField('extraBody', e.target.value)}
                placeholder={'{"provider":{"order":["Baidu"],"allow_fallbacks":false}}'}
                rows={4}
                spellCheck={false}
                style={{
                  padding: '12px 16px',
                  lineHeight: 1.6,
                  fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace',
                  fontSize: 12.5,
                  resize: 'vertical',
                }}
              />
              <div
                style={{
                  marginTop: 7,
                  fontSize: 11.5,
                  lineHeight: 1.7,
                  color: extraBodyError ? 'var(--warn)' : 'var(--text-dim-2)',
                }}
              >
                {extraBodyError ??
                  '原样合并进上游请求体，用于 provider 路由这类各家网关的私有参数。留空表示不追加。'}
              </div>
            </div>
          )}
        </div>

        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexWrap: 'wrap',
            marginTop: 20,
          }}
        >
          <button
            className="btn-ghost"
            onClick={onTest}
            disabled={actionLocked || !!extraBodyError}
            style={{ fontSize: 12.5, padding: '9px 18px' }}
          >
            {testing ? '正在测试…' : '测试连接'}
          </button>
          <button
            className="btn-primary"
            onClick={onSave}
            disabled={!canSave || actionLocked}
            style={{ fontSize: 12.5, padding: '9px 20px' }}
          >
            {saving ? '正在保存…' : '保存并生效'}
          </button>
          {/* 已生效时左栏状态点已说明一切，这里不再重复 */}
          {dirty && (
            <span style={{ fontSize: 11.5, color: 'var(--text-dim-3)', lineHeight: 1.6 }}>
              {tested ? '连接测试已通过，可保存生效' : '需先通过连接测试才能保存'}
            </span>
          )}
        </div>

        {showRebuild && (
          <div className="note-warn" style={{ marginTop: 16 }}>
            <div style={{ fontSize: 12.5, color: 'var(--warn)', lineHeight: 1.7, fontWeight: 600 }}>
              ⚠ Embedding 模型或向量维度已变更
            </div>
            <div
              style={{ fontSize: 12, color: 'var(--accent-pale)', lineHeight: 1.7, marginTop: 6 }}
            >
              必须重建世界书与长期记忆索引，重建完成前不会使用旧向量检索。
            </div>
            <button
              onClick={onRebuild}
              disabled={actionLocked}
              style={{
                marginTop: 12,
                fontSize: 12.5,
                fontWeight: 600,
                color: 'var(--on-accent)',
                background: 'linear-gradient(135deg,#e0b96a,#e2704e)',
                border: 'none',
                padding: '9px 18px',
                borderRadius: 20,
                cursor: actionLocked ? 'not-allowed' : 'pointer',
                opacity: actionLocked ? 0.65 : 1,
                fontFamily: 'inherit',
                whiteSpace: 'nowrap',
              }}
            >
              {rebuilding ? '正在重建索引…' : '立即重建索引'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
