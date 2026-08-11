import { useEffect, useRef, useState } from 'react';

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
  const streaming = useAppStore((s) => s.streaming);
  const streamRetrieved = useAppStore((s) => s.streamRetrieved);
  const chatStyle = useAppStore((s) => s.chatStyle);
  const showRagHints = useAppStore((s) => s.showRagHints);
  const identity = useAppStore((s) => s.identity);
  const renameSession = useAppStore((s) => s.renameSession);
  const send = useAppStore((s) => s.send);

  const hasSession = !!session;
  const characterName = character?.name ?? '';

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
    if (!text || streaming) return;
    setDraft('');
    void send(text);
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
                还没有对话
              </div>
              <div style={{ fontSize: 12.5 }}>点击左侧「＋ 新建对话」，选择角色与世界书后开始</div>
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
              disabled={!hasSession}
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
              disabled={!hasSession || !!streaming}
              style={{
                flex: 'none',
                width: 40,
                height: 40,
                borderRadius: 12,
                border: 'none',
                background: hasSession ? 'var(--grad-accent)' : 'rgba(255,214,170,.08)',
                color: hasSession ? 'var(--on-accent)' : 'var(--text-dim-4)',
                fontSize: 17,
                cursor: hasSession && !streaming ? 'pointer' : 'not-allowed',
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
}

function MessageRow({
  message,
  characterName,
  userName,
  chatStyle,
  showRagHints,
  retrievedOverride,
  streaming = false,
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
