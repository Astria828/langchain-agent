import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { BookIcon } from '@/components/icons';
import { useAppStore } from '@/stores/appStore';

/**
 * 选择角色、世界书或「不绑定世界书」（设计稿第 6685–6733 行）。
 * 角色卡为必选，世界书为可选；选择不绑定时后续对话不执行任何世界书检索与注入。
 */

const NO_BOOK = '__none__';

export default function NewSessionPage() {
  const navigate = useNavigate();
  const characters = useAppStore((s) => s.characters);
  const contentLoading = useAppStore((s) => s.contentLoading);
  const contentError = useAppStore((s) => s.contentError);
  const loadContent = useAppStore((s) => s.loadContent);
  const worldBooks = useAppStore((s) => s.worldBooks);
  const worldBooksLoading = useAppStore((s) => s.worldBooksLoading);
  const worldBooksError = useAppStore((s) => s.worldBooksError);
  const loadWorldBooks = useAppStore((s) => s.loadWorldBooks);
  const identity = useAppStore((s) => s.identity);
  const startSession = useAppStore((s) => s.startSession);

  const [pickedCharacter, setPickedCharacter] = useState(() => characters[0]?.id ?? '');
  const [pickedBook, setPickedBook] = useState(() => worldBooks[0]?.id ?? NO_BOOK);
  const [busy, setBusy] = useState(false);

  // 真实内容由后端异步加载，确保初始空数组不会留下无效选择。
  useEffect(() => {
    if (!characters.some((character) => character.id === pickedCharacter)) {
      setPickedCharacter(characters[0]?.id ?? '');
    }
  }, [characters, pickedCharacter]);

  useEffect(() => {
    if (
      pickedBook !== NO_BOOK &&
      !worldBooks.some((worldBook) => worldBook.id === pickedBook)
    ) {
      setPickedBook(worldBooks[0]?.id ?? NO_BOOK);
    }
  }, [pickedBook, worldBooks]);

  const selectedStyle = (on: boolean) => ({
    border: `1px solid ${on ? 'rgba(240,163,94,.6)' : 'var(--line-2)'}`,
    boxShadow: on ? '0 0 0 1px rgba(240,163,94,.4), 0 4px 30px rgba(240,163,94,.12)' : 'none',
  });

  const start = async () => {
    if (!pickedCharacter || busy) return;
    setBusy(true);
    const session = await startSession({
      characterId: pickedCharacter,
      worldBookId: pickedBook === NO_BOOK ? null : pickedBook,
    });
    setBusy(false);
    if (session) navigate('/chat');
  };

  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '52px 56px' }}>
      <div style={{ maxWidth: 860, margin: '0 auto' }}>
        <div className="serif" style={{ fontSize: 28, fontWeight: 600 }}>
          开启新的故事
        </div>
        <div style={{ fontSize: 13.5, color: 'var(--text-dim)', marginTop: 8, lineHeight: 1.7 }}>
          选择角色卡与世界书的组合。创建后身份与角色配置会保存为独立会话快照。
        </div>

        {contentError && (
          <div className="note-warn" style={{ marginTop: 24 }}>
            身份与角色卡加载失败：{contentError}
            <button
              className="btn-ghost"
              onClick={() => void loadContent()}
              disabled={contentLoading}
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              重试
            </button>
          </div>
        )}
        {worldBooksError && (
          <div className="note-warn" style={{ marginTop: 12 }}>
            世界书加载失败：{worldBooksError}
            <button
              className="btn-ghost"
              onClick={() => void loadWorldBooks()}
              disabled={worldBooksLoading}
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              重试
            </button>
          </div>
        )}

        <div style={{ fontSize: 12, letterSpacing: '.18em', color: 'var(--accent-deep)', margin: '38px 0 16px' }}>
          ① 选择角色卡
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 16 }}>
          {characters.map((c) => (
            <div
              key={c.id}
              className="card-pick"
              onClick={() => setPickedCharacter(c.id)}
              style={{ padding: 20, ...selectedStyle(pickedCharacter === c.id) }}
            >
              <div style={{ minWidth: 0, pointerEvents: 'none' }}>
                <div className="serif" style={{ fontSize: 16, fontWeight: 600 }}>
                  {c.name}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-dim-2)', marginTop: 3 }}>
                  {c.introduction}
                </div>
              </div>
              <div
                style={{
                  fontSize: 12.5,
                  color: 'var(--text-mute)',
                  lineHeight: 1.75,
                  marginTop: 14,
                  pointerEvents: 'none',
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                }}
              >
                {c.systemPrompt}
              </div>
            </div>
          ))}
          {contentLoading && characters.length === 0 && (
            <div style={{ fontSize: 13, color: 'var(--text-dim-3)' }}>正在读取角色卡…</div>
          )}
          {!contentLoading && !contentError && characters.length === 0 && (
            <div style={{ fontSize: 13, color: 'var(--text-dim-3)' }}>
              还没有角色卡，请先在角色卡页面创建。
            </div>
          )}
        </div>

        <div style={{ fontSize: 12, letterSpacing: '.18em', color: 'var(--accent-deep)', margin: '38px 0 16px' }}>
          ② 绑定世界书（可选）
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 16 }}>
          {worldBooks.map((b) => (
            <div
              key={b.id}
              className="card-pick"
              onClick={() => setPickedBook(b.id)}
              style={{ padding: 20, ...selectedStyle(pickedBook === b.id) }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, pointerEvents: 'none' }}>
                <BookIcon size={17} stroke="var(--accent-deep)" />
                <span className="serif" style={{ fontSize: 15.5, fontWeight: 600 }}>
                  {b.name}
                </span>
              </div>
              <div
                style={{
                  fontSize: 12.5,
                  color: 'var(--text-mute)',
                  marginTop: 10,
                  pointerEvents: 'none',
                }}
              >
                {b.entries.length} 个条目 · 创建会话时保存快照
              </div>
            </div>
          ))}
          {worldBooksLoading && worldBooks.length === 0 && (
            <div style={{ fontSize: 13, color: 'var(--text-dim-3)' }}>正在读取世界书…</div>
          )}
          <div
            className="card-pick"
            onClick={() => setPickedBook(NO_BOOK)}
            style={{ padding: 20, ...selectedStyle(pickedBook === NO_BOOK) }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, pointerEvents: 'none' }}>
              <span style={{ fontSize: 16, color: 'var(--text-dim-2)' }}>○</span>
              <span className="serif" style={{ fontSize: 15.5, fontWeight: 600 }}>
                不绑定世界书
              </span>
            </div>
            <div
              style={{
                fontSize: 12.5,
                color: 'var(--text-mute)',
                marginTop: 10,
                pointerEvents: 'none',
              }}
            >
              纯角色对话，不启用世界观检索
            </div>
          </div>
        </div>

        <div style={{ fontSize: 12, letterSpacing: '.18em', color: 'var(--accent-deep)', margin: '38px 0 16px' }}>
          ③ 确认你的身份
        </div>
        <div
          className="card"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            padding: '18px 22px',
            borderRadius: 16,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14.5, fontWeight: 600 }}>{identity?.name}</div>
            <div style={{ fontSize: 12.5, color: 'var(--text-dim-2)', marginTop: 3 }}>
              {identity?.personaName}
            </div>
          </div>
          <button className="btn-ghost" onClick={() => navigate('/identity')} style={{ fontSize: 12.5, padding: '7px 16px' }}>
            编辑身份
          </button>
        </div>

        <button
          className="btn-primary"
          onClick={() => void start()}
          disabled={
            !pickedCharacter ||
            !identity ||
            busy ||
            contentLoading ||
            !!contentError ||
            ((worldBooksLoading || !!worldBooksError) && pickedBook !== NO_BOOK)
          }
          style={{
            marginTop: 40,
            width: '100%',
            padding: 16,
            borderRadius: 16,
            fontSize: 16,
            fontWeight: 700,
            fontFamily: 'var(--font-serif)',
            letterSpacing: '.2em',
            boxShadow: '0 4px 30px rgba(240,163,94,.3)',
          }}
        >
          {busy ? '正在创建…' : '开始新会话 ✧'}
        </button>
        <div style={{ height: 40 }} />
      </div>
    </div>
  );
}
