import { useEffect } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import ResizableDivider, { useResizablePanel } from '@/components/ResizableDivider';
import Sidebar from '@/components/Sidebar';
import Toast from '@/components/Toast';
import { useAppStore } from '@/stores/appStore';

import ChatPage from '@/pages/ChatPage';
import NewSessionPage from '@/pages/NewSessionPage';
import CharactersPage from '@/pages/CharactersPage';
import WorldBooksPage from '@/pages/WorldBooksPage';
import MemoriesPage from '@/pages/MemoriesPage';
import IdentityPage from '@/pages/IdentityPage';
import DataPage from '@/pages/DataPage';
import ApiSettingsPage from '@/pages/ApiSettingsPage';
import LogsPage from '@/pages/LogsPage';

export default function App() {
  const bootstrap = useAppStore((s) => s.bootstrap);
  const bootstrapped = useAppStore((s) => s.bootstrapped);
  const sidebar = useResizablePanel({
    storageKey: 'loreweave.layout.sidebarWidth',
    defaultWidth: 224,
    minWidth: 188,
    maxWidth: 320,
  });

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  return (
    <div
      style={{
        display: 'flex',
        height: '100vh',
        overflow: 'hidden',
        background:
          'radial-gradient(1200px 700px at 75% -10%, rgba(240,163,94,.09), transparent 60%), radial-gradient(900px 600px at -5% 100%, rgba(226,112,78,.07), transparent 55%), #161009',
        color: 'var(--text)',
      }}
    >
      <Sidebar width={sidebar.width} />
      <ResizableDivider
        width={sidebar.width}
        defaultWidth={224}
        minWidth={188}
        maxWidth={320}
        minRemainingWidth={480}
        label="调整全局导航栏宽度"
        onResize={sidebar.setWidth}
      />

      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', position: 'relative' }}>
        {bootstrapped ? (
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/session" element={<NewSessionPage />} />
            <Route path="/characters" element={<CharactersPage />} />
            <Route path="/worldbooks" element={<WorldBooksPage />} />
            <Route path="/memories" element={<MemoriesPage />} />
            <Route path="/identity" element={<IdentityPage />} />
            <Route path="/data" element={<DataPage />} />
            {/* 避开 /api*：那是转发到后端的代理前缀 */}
            <Route path="/settings" element={<ApiSettingsPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        ) : (
          <div
            style={{
              height: '100%',
              display: 'grid',
              placeItems: 'center',
              color: 'var(--text-dim-3)',
              fontSize: 13,
            }}
          >
            正在载入…
          </div>
        )}
        <Toast />
      </main>
    </div>
  );
}
