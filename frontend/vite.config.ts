import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 阶段 5 已固定将会话与实时对话请求转发到真实后端。
      // 用正则精确匹配 /api/ 开头的请求，避免把前端路由（如 /api-xxx）误当成后端调用转发出去。
      '^/api/': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
