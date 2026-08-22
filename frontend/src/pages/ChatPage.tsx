import { useEffect, useRef, useState } from 'react';

import MessageBlocks from '@/components/MessageBlocks';
import ResizableDivider, { useResizablePanel } from '@/components/ResizableDivider';
import SessionList from '@/components/SessionList';
import {
  ContinueIcon,
  PanelIcon,
  RegenerateIcon,
  SendIcon,
  SparkIcon,
  TrashIcon,
} from '@/components/icons';
import {
  selectCurrentCharacter,
  selectCurrentSession,
  selectCurrentWorldBook,
  useAppStore,
} from '@/stores/appStore';
import type { Message } from '@/types';

/** 对话工作台与流式回复（设计稿第 6561–6683 行） */

/** 面板开关的三段循环：全部显示 → 收起对话列表 → 再收起导航栏 → 回到全部显示 */
const PANEL_STAGE_COUNT = 3;
const PANEL_STAGE_HINT = ['隐藏对话列表', '隐藏左侧导航栏', '恢复全部面板'];

export default function ChatPage() {
  const [panelStage, setPanelStage] = useState(0);
  const [draft, setDraft] = useState('');
  const [titleDraft, setTitleDraft] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [deletingTurn, setDeletingTurn] = useState(false);
  const [recommending, setRecommending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sessionPanel = useResizablePanel({
    storageKey: 'loreweave.layout.sessionListWidth',
    defaultWidth: 274,
    minWidth: 220,
    maxWidth: 440,
  });

  const session = useAppStore(selectCurrentSession);
  const character = useAppStore(selectCurrentCharacter);
  const worldBook = useAppStore(selectCurrentWorldBook);
  const messages = useAppStore((s) => s.messages);
  const sessionsLoading = useAppStore((s) => s.sessionsLoading);
  const sessionsError = useAppStore((s) => s.sessionsError);
  const loadSessions = useAppStore((s) => s.loadSessions);
  const streaming = useAppStore((s) => s.streaming);
  const streamRetrieved = useAppStore((s) => s.streamRetrieved);
  const messageActionPending = useAppStore((s) => s.messageActionPending);
  const chatStyle = useAppStore((s) => s.chatStyle);
  const showRagHints = useAppStore((s) => s.showRagHints);
  const identity = useAppStore((s) => s.identity);
  const renameSession = useAppStore((s) => s.renameSession);
  const removeTurn = useAppStore((s) => s.removeTurn);
  const removeMessage = useAppStore((s) => s.removeMessage);
  const runMessageAction = useAppStore((s) => s.runMessageAction);
  const recommendReply = useAppStore((s) => s.recommendReply);
  const send = useAppStore((s) => s.send);
  const setNavCollapsed = useAppStore((s) => s.setNavCollapsed);

  const listOpen = panelStage === 0;
  const cyclePanels = () => {
    const next = (panelStage + 1) % PANEL_STAGE_COUNT;
    setPanelStage(next);
    setNavCollapsed(next === 2);
  };

  // 离开对话页时把全局导航栏放回来，避免在别的页面没有入口
  useEffect(() => () => setNavCollapsed(false), [setNavCollapsed]);

  const hasSession = !!session;
  const characterName = character?.name ?? '';
  const busy = !!streaming || !!messageActionPending || deletingTurn || recommending;
  const latestMessage = messages[messages.length - 1];
  const previousMessage = messages[messages.length - 2];
  const actionableMessageId =
    session && latestMessage?.role === 'assistant' && previousMessage?.role === 'user'
      ? latestMessage.id
      : null;

  const chatMeta = session
    ? `${characterName} · ${worldBook?.name ?? '未绑定世界书'} · 基础对话`
    : '新建对话后开始角色扮演';

  useEffect(() => {
    setTitleDraft(session?.title ?? '');
  }, [session?.id, session?.title]);

  // 新消息或流式增量到达时贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft('');
    void send(text);
  };

  const fillRecommendedReply = async () => {
    if (draft.trim() || busy) return;
    setRecommending(true);
    try {
      const content = await recommendReply();
      if (content) setDraft(content);
    } finally {
      setRecommending(false);
    }
  };

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0 }}>
      {listOpen && (
        <>
          <SessionList width={sessionPanel.width} />
          <ResizableDivider
            width={sessionPanel.width}
            defaultWidth={274}
            minWidth={220}
            maxWidth={440}
            minRemainingWidth={280}
            label="调整会话列表宽度"
            onResize={sessionPanel.setWidth}
          />
        </>
      )}

      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <header
          style={{
            flex: 'none',
            display: 'flex',
            alignItems: 'center',
            gap: 14,
            padding: '16px 30px',
            borderBottom: '1px solid var(--line)',
            backdropFilter: 'blur(8px)',
            flexWrap: 'nowrap',
            overflow: 'hidden',
          }}
        >
          <button
            className="icon-plain"
            onClick={cyclePanels}
            title={PANEL_STAGE_HINT[panelStage]}
            style={{ flex: 'none', width: 28, height: 28 }}
          >
            <PanelIcon size={20} />
          </button>
          <div style={{ flex: '1 1 auto', minWidth: 120 }}>
            <input
              className="title-input"
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={() => {
                if (!session) return;
                void renameSession(session.id, titleDraft).then((saved) => {
                  if (!saved) setTitleDraft(session.title);
                });
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') e.currentTarget.blur();
              }}
              placeholder={hasSession ? '未命名对话' : '还没有对话'}
              disabled={!hasSession}
            />
            <div
              style={{
                fontSize: 12,
                color: 'var(--text-dim-2)',
                marginTop: 2,
                paddingLeft: 1,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {chatMeta}
            </div>
          </div>
        </header>

        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '40px 0 28px' }}>
          {!hasSession && (
            <div
              style={{
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 14,
                color: 'var(--text-dim-3)',
              }}
            >
              <div className="serif" style={{ fontSize: 16 }}>
                {sessionsLoading ? '正在读取对话…' : sessionsError ? '对话加载失败' : '还没有对话'}
              </div>
              <div style={{ fontSize: 12.5 }}>
                {sessionsError ?? '点击左侧「＋ 新建对话」，选择角色与世界书后开始'}
              </div>
              {sessionsError && (
                <button
                  className="btn-ghost"
                  onClick={() => void loadSessions()}
                  disabled={sessionsLoading}
                  style={{ fontSize: 12.5, padding: '8px 16px' }}
                >
                  {sessionsLoading ? '正在重试…' : '重试'}
                </button>
              )}
            </div>
          )}

          {hasSession && (
            <div
              style={{
                width: '100%',
                boxSizing: 'border-box',
                padding: '0 clamp(48px, 6vw, 120px) 0 30px',
                display: 'flex',
                flexDirection: 'column',
                gap: 30,
              }}
            >
              {messages.map((message) => (
                <MessageRow
                  key={message.id}
                  message={message}
                  characterName={characterName}
                  userName={identity?.name ?? ''}
                  chatStyle={chatStyle}
                  showRagHints={showRagHints}
                  streaming={messageActionPending?.messageId === message.id}
                  actions={
                    message.id === actionableMessageId
                      ? {
                          pendingAction:
                            messageActionPending?.messageId === message.id
                              ? messageActionPending.action
                              : null,
                          confirmDelete: deleteConfirmId === message.id,
                          disabled: busy,
                          onRegenerate: () => void runMessageAction(message.id, 'regenerate'),
                          onContinue: () => void runMessageAction(message.id, 'continue'),
                          onDeleteRequest: () => setDeleteConfirmId(message.id),
                          onDeleteCancel: () => setDeleteConfirmId(null),
                          onDeleteConfirm: () => {
                            setDeletingTurn(true);
                            void removeTurn(message.id).finally(() => {
                              setDeletingTurn(false);
                              setDeleteConfirmId(null);
                            });
                          },
                        }
                      : undefined
                  }
                  danglingActions={
                    message.unanswered
                      ? {
                          confirmDelete: deleteConfirmId === message.id,
                          disabled: busy,
                          onDeleteRequest: () => setDeleteConfirmId(message.id),
                          onDeleteCancel: () => setDeleteConfirmId(null),
                          onDeleteConfirm: () => {
                            setDeletingTurn(true);
                            void removeMessage(message.id).finally(() => {
                              setDeletingTurn(false);
                              setDeleteConfirmId(null);
                            });
                          },
                        }
                      : undefined
                  }
                />
              ))}
              {streaming && (
                <MessageRow
                  message={streaming}
                  characterName={characterName}
                  userName={identity?.name ?? ''}
                  chatStyle={chatStyle}
                  showRagHints={showRagHints}
                  retrievedOverride={streamRetrieved}
                  streaming
                />
              )}
            </div>
          )}
        </div>

        <div
          style={{
            flex: 'none',
            padding: '18px clamp(48px, 6vw, 120px) 26px 30px',
          }}
        >
          <div
            style={{
              width: '100%',
              boxSizing: 'border-box',
              display: 'flex',
              gap: 12,
              // 三者共用同一条水平中线，输入区变高时按钮不会被拖到底部
              alignItems: 'center',
              padding: '9px 16px',
              borderRadius: 14,
              background: hasSession ? 'rgba(36,26,18,.85)' : 'rgba(30,22,15,.5)',
              border: '1px solid var(--line-4)',
              boxShadow: '0 8px 40px rgba(0,0,0,.4)',
            }}
            >
            <button
              className="compose-action"
              onClick={() => void fillRecommendedReply()}
              disabled={!hasSession || busy || !!draft.trim()}
              title={draft.trim() ? '请先清空输入框' : '生成一句可编辑的用户回复'}
            >
              <SparkIcon />
              {recommending ? '生成中…' : '推荐回复'}
            </button>
            <textarea
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              disabled={!hasSession || busy}
              placeholder={hasSession ? '' : '请先新建一个对话'}
              style={{
                flex: 1,
                resize: 'none',
                background: 'transparent',
                border: 'none',
                color: 'var(--text)',
                fontFamily: 'var(--font-serif)',
                fontSize: 15,
                lineHeight: 1.7,
                minHeight: 24,
                maxHeight: 120,
              }}
            />
            <button
              onClick={submit}
              disabled={!hasSession || busy}
              style={{
                flex: 'none',
                width: 34,
                height: 34,
                borderRadius: 10,
                border: 'none',
                background: hasSession ? 'var(--grad-accent)' : 'rgba(255,214,170,.08)',
                color: hasSession ? 'var(--on-accent)' : 'var(--text-dim-4)',
                display: 'grid',
                placeItems: 'center',
                cursor: hasSession && !busy ? 'pointer' : 'not-allowed',
                boxShadow: hasSession ? '0 0 18px rgba(240,163,94,.35)' : 'none',
              }}
              title="发送"
            >
              {/* 纸飞机偏向右上，向左下各让 0.5px 抵消视觉重心 */}
              <SendIcon style={{ marginRight: 0.5, marginTop: 0.5 }} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  message: Message;
  characterName: string;
  userName: string;
  chatStyle: '沉浸叙事' | '经典气泡';
  showRagHints: boolean;
  retrievedOverride?: string[];
  streaming?: boolean;
  actions?: {
    pendingAction: 'regenerate' | 'continue' | null;
    confirmDelete: boolean;
    disabled: boolean;
    onRegenerate: () => void;
    onContinue: () => void;
    onDeleteRequest: () => void;
    onDeleteCancel: () => void;
    onDeleteConfirm: () => void;
  };
  /** 断层用户消息的单独删除入口，仅在 message.unanswered 时提供 */
  danglingActions?: {
    confirmDelete: boolean;
    disabled: boolean;
    onDeleteRequest: () => void;
    onDeleteCancel: () => void;
    onDeleteConfirm: () => void;
  };
}

function MessageRow({
  message,
  characterName,
  userName,
  chatStyle,
  showRagHints,
  retrievedOverride,
  streaming = false,
  actions,
  danglingActions,
}: RowProps) {
  const bubble = chatStyle === '经典气泡';

  if (message.role === 'memo') {
    const text = message.blocks.map((b) => b.content).join('');
    return (
      <div style={{ animation: 'fadeUp .35s ease both' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '2px 0' }}>
          <div
            style={{
              flex: 1,
              height: 1,
              background: 'linear-gradient(90deg,transparent,rgba(143,214,160,.25))',
            }}
          />
          <span
            style={{
              fontSize: 11.5,
              color: 'var(--ok)',
              display: 'flex',
              gap: 6,
              alignItems: 'center',
            }}
          >
            ✦ {text}
          </span>
          <div
            style={{
              flex: 1,
              height: 1,
              background: 'linear-gradient(90deg,rgba(143,214,160,.25),transparent)',
            }}
          />
        </div>
      </div>
    );
  }

  if (message.role === 'assistant') {
    const retrieved = retrievedOverride ?? message.retrieved ?? [];
    return (
      <div style={{ animation: 'fadeUp .35s ease both' }}>
        <div style={{ borderLeft: '2px solid rgba(240,163,94,.45)', padding: '2px 0 2px 20px' }}>
          {showRagHints && retrieved.length > 0 && (
            <div
              style={{
                fontSize: 11,
                color: 'var(--speaker-user)',
                marginBottom: 8,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <span style={{ color: 'var(--accent-deep)' }}>⌕</span>
              检索到世界书条目「{retrieved.join('」「')}」
            </div>
          )}
          <div
            style={{
              fontSize: 12,
              letterSpacing: '.16em',
              color: 'var(--speaker-char)',
              marginBottom: 8,
              fontWeight: 600,
            }}
          >
            {characterName}
          </div>
          {streaming && message.blocks.length === 0 && (
            <div className="thinking-row">
              <span className="thinking-label">思考中</span>
            </div>
          )}
          <MessageBlocks blocks={message.blocks} chatStyle={chatStyle} streaming={streaming} />
          {actions && (
            <div style={{ marginTop: 10 }}>
              {!actions.confirmDelete ? (
                <div className="act-row">
                  <button className="act-btn" onClick={actions.onRegenerate} disabled={actions.disabled}>
                    <RegenerateIcon />
                    {actions.pendingAction === 'regenerate' ? '正在重说…' : '重说'}
                  </button>
                  <span className="act-sep" />
                  <button className="act-btn" onClick={actions.onContinue} disabled={actions.disabled}>
                    <ContinueIcon />
                    {actions.pendingAction === 'continue' ? '正在继续…' : '继续说'}
                  </button>
                  <span className="act-sep" />
                  <button
                    className="act-btn act-btn--danger"
                    onClick={actions.onDeleteRequest}
                    disabled={actions.disabled}
                  >
                    <TrashIcon size={13} strokeWidth={1.5} />
                    删除本轮
                  </button>
                </div>
              ) : (
                // 就地降级为一行文字确认，避免弹出整块卡片造成布局跳动
                <div className="act-row">
                  <span className="act-note">删除本轮？用户消息与角色回复将一并永久删除。</span>
                  <button className="act-btn act-btn--confirm" onClick={actions.onDeleteConfirm}>
                    确认删除
                  </button>
                  <span className="act-sep" />
                  <button className="act-btn" onClick={actions.onDeleteCancel}>
                    取消
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  const text = message.blocks.map((b) => b.content).join('');

  // 这条消息发出后没能等到回复。保留原文，另给一个单独删除的出口，
  // 否则它会永远卡在历史里：整轮删除需要一条配对的角色回复才能触发。
  const danglingNote = danglingActions && (
    <div style={{ marginTop: 8, display: 'flex', justifyContent: bubble ? 'flex-end' : undefined }}>
      {!danglingActions.confirmDelete ? (
        <div className="act-row">
          <span className="act-note">这条消息没有收到回复。</span>
          <button
            className="act-btn act-btn--danger"
            onClick={danglingActions.onDeleteRequest}
            disabled={danglingActions.disabled}
          >
            <TrashIcon size={13} strokeWidth={1.5} />
            删除这条
          </button>
        </div>
      ) : (
        <div className="act-row">
          <span className="act-note">删除这条消息？原文将被永久删除。</span>
          <button className="act-btn act-btn--confirm" onClick={danglingActions.onDeleteConfirm}>
            确认删除
          </button>
          <span className="act-sep" />
          <button className="act-btn" onClick={danglingActions.onDeleteCancel}>
            取消
          </button>
        </div>
      )}
    </div>
  );

  return (
    <div style={{ animation: 'fadeUp .35s ease both' }}>
      {bubble ? (
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <div
            style={{
              maxWidth: '80%',
              padding: '14px 18px',
              borderRadius: '18px 4px 18px 18px',
              background: 'linear-gradient(135deg,rgba(240,163,94,.16),rgba(226,112,78,.12))',
              border: '1px solid rgba(240,163,94,.22)',
              fontSize: 14.5,
              lineHeight: 1.8,
              color: '#f5e6cf',
            }}
          >
            {text}
          </div>
        </div>
      ) : null}
      {bubble ? null : (
        <div style={{ padding: '2px 0' }}>
          <div
            style={{
              fontSize: 12,
              letterSpacing: '.16em',
              color: 'var(--speaker-user)',
              marginBottom: 6,
              fontWeight: 600,
            }}
          >
            {userName}
          </div>
          <div
            className="serif"
            style={{ fontSize: 15, lineHeight: 1.95, color: 'var(--text-soft)' }}
          >
            {text}
          </div>
        </div>
      )}
      {danglingNote}
    </div>
  );
}
