import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

// 设计稿使用 Noto Sans SC（正文）与 Noto Serif SC（标题、台词、动作斜体）。
// 只取简中与拉丁子集，跳过西里尔/越南语分片，避免 800KB+ 的 @font-face 声明进 CSS。
import '@fontsource/noto-sans-sc/chinese-simplified-400.css';
import '@fontsource/noto-sans-sc/chinese-simplified-500.css';
import '@fontsource/noto-sans-sc/chinese-simplified-600.css';
import '@fontsource/noto-sans-sc/chinese-simplified-700.css';
import '@fontsource/noto-sans-sc/latin-400.css';
import '@fontsource/noto-sans-sc/latin-500.css';
import '@fontsource/noto-sans-sc/latin-600.css';
import '@fontsource/noto-sans-sc/latin-700.css';
import '@fontsource/noto-serif-sc/chinese-simplified-400.css';
import '@fontsource/noto-serif-sc/chinese-simplified-600.css';
import '@fontsource/noto-serif-sc/chinese-simplified-700.css';
import '@fontsource/noto-serif-sc/latin-400.css';
import '@fontsource/noto-serif-sc/latin-600.css';
import '@fontsource/noto-serif-sc/latin-700.css';

import './styles/globals.css';
import App from './App';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
