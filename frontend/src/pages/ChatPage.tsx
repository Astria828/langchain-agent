import { useEffect, useRef, useState } from 'react';

import ConfirmInline from '@/components/ConfirmInline';
import MessageBlocks from '@/components/MessageBlocks';
import ResizableDivider, { useResizablePanel } from '@/components/ResizableDivider';
import SessionList from '@/components/SessionList';
import { PanelIcon } from '@/components/icons';
import {
  selectContextHint,
  selectCurrentCharacter,
  selectCurrentSession,
  selectCurrentWorldBook,
  useAppStore,
} from '@/stores/appStore';
import type { Message } from '@/types';

/** 对话工作台与流式回复（设计稿第 6561–6683 行） */

export default function ChatPage() {
  const [listOpen, setListOpen] = useState(true);
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
  const contextHint = useAppStore(selectContextHint);
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
  const runMessageAction = useAppStore((s) => s.runMessageAction);
  const recommendReply = useAppStore((s) => s.recommendReply);
  const send = useAppStore((s) => s.send);

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
            onClick={() => setListOpen((v) => !v)}
            title="显示/隐藏对话列表"
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
              alignItems: 'flex-end',
              padding: '14px 16px',
              borderRadius: 18,
              background: hasSession ? 'rgba(36,26,18,.85)' : 'rgba(30,22,15,.5)',
              border: '1px solid var(--line-4)',
              boxShadow: '0 8px 40px rgba(0,0,0,.4)',
            }}
            >
            <button
              className="btn-ghost"
              onClick={() => void fillRecommendedReply()}
              disabled={!hasSession || busy || !!draft.trim()}
              title={draft.trim() ? '请先清空输入框' : '生成一句可编辑的用户回复'}
              style={{ flex: 'none', fontSize: 12, padding: '8px 10px' }}
            >
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
              placeholder={hasSession ? '以你的身份继续故事…（Enter 发送）' : '请先新建一个对话'}
              style={{
                flex: 1,
                resize: 'none',
                background: 'transparent',
                border: 'none',
                color: 'var(--text)',
                fontFamily: 'var(--font-serif)',
                fontSize: 15,
                lineHeight: 1.7,
                minHeight: 28,
                maxHeight: 120,
              }}
            />
            <button
              onClick={submit}
              disabled={!hasSession || busy}
              style={{
                flex: 'none',
                width: 40,
                height: 40,
                borderRadius: 12,
                border: 'none',
                background: hasSession ? 'var(--grad-accent)' : 'rgba(255,214,170,.08)',
                color: hasSession ? 'var(--on-accent)' : 'var(--text-dim-4)',
                fontSize: 17,
                cursor: hasSession && !busy ? 'pointer' : 'not-allowed',
                boxShadow: hasSession ? '0 0 18px rgba(240,163,94,.35)' : 'none',
              }}
            >
              ↑
            </button>
          </div>
          <div
            style={{
              width: '100%',
              marginTop: 8,
              fontSize: 11,
              color: 'var(--text-dim-4)',
              textAlign: 'center',
            }}
          >
            {contextHint}
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
          <MessageBlocks blocks={message.blocks} chatStyle={chatStyle} streaming={streaming} />
          {actions && (
            <div style={{ marginTop: 12 }}>
              {!actions.confirmDelete && (
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <button
                    className="btn-ghost"
                    onClick={actions.onRegenerate}
                    disabled={actions.disabled}
                    style={{ fontSize: 11.5, padding: '6px 10px' }}
                  >
                    {actions.pendingAction === 'regenerate' ? '正在重说…' : '重说'}
                  </button>
                  <button
                    className="btn-ghost"
                    onClick={actions.onContinue}
                    disabled={actions.disabled}
                    style={{ fontSize: 11.5, padding: '6px 10px' }}
                  >
                    {actions.pendingAction === 'continue' ? '正在继续…' : '继续说'}
                  </button>
                  <button
                    className="btn-cancel"
                    onClick={actions.onDeleteRequest}
                    disabled={actions.disabled}
                    style={{ fontSize: 11.5, padding: '6px 10px' }}
                  >
                    删除本轮
                  </button>
                </div>
              )}
              {actions.confirmDelete && (
                <ConfirmInline
                  layout="inline"
                  text="该轮用户消息和角色回复将被永久删除。"
                  onConfirm={actions.onDeleteConfirm}
                  onCancel={actions.onDeleteCancel}
                  style={{ marginTop: 4 }}
                />
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  const text = message.blocks.map((b) => b.content).join('');

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
      ) : (
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
    </div>
  );
}
