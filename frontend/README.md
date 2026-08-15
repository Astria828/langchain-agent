# 织境 LoreWeave · 前端

依据《PRD》《项目架构图》《项目文件架构图》《开发计划书》，将交互设计稿
`织境 LoreWeave.html` 复刻为 React + TypeScript 工程。

## 启动

```bash
npm ci
npm run dev
```

打开 http://localhost:5173。九个页面均固定访问真实后端，请先启动根目录 README 中的 FastAPI 服务。

其他命令：`npm run build`（含类型检查）、`npm run typecheck`。

## 后端连接

开发服务器由 `vite.config.ts` 将 `/api/` 请求代理到 `http://localhost:8000`。阶段 9 已移除运行时 Mock 选择逻辑，`.env.example` 不再要求配置开关；`services/mock/` 仅保留为早期设计数据参考，不参与应用运行。

## 目录

对齐《项目文件架构图》§1，仅在 `services/` 下多出 `mock/`：

```
src/
├── App.tsx              布局、侧边栏与页面路由
├── pages/               九个页面，与设计稿导航一一对应
├── components/          Sidebar / MessageBlocks / SessionList 及复用件
├── services/
│   ├── api.ts           REST 封装、ApiClient 契约与 mock 开关
│   ├── chatStream.ts    SSE 客户端（fetch + ReadableStream）
│   └── mock/            seed.ts 示例数据、mockApi.ts 本地实现
├── stores/appStore.ts   身份、角色、世界书、会话与派生选择器
├── types/index.ts       DTO 与 action/dialogue 内容块类型
└── styles/globals.css   主题变量、reset、动作斜体与复用类
```

## 路由

| 路径 | 页面 |
| --- | --- |
| `/chat` | 对话 |
| `/session` | 新会话 |
| `/characters` | 角色卡 |
| `/worldbooks` | 世界书 |
| `/memories` | 记忆 |
| `/identity` | 我的身份 |
| `/settings` | API 配置 |
| `/data` | 数据管理 |
| `/logs` | 系统日志 |

API 配置页用 `/settings` 而非 `/api`：后者会与转发给后端的 `/api` 代理前缀冲突。

## 与设计稿的差异

设计稿源码已解包到 `../doc/ui-reference.html`，可逐页对照。有意为之的差异：

1. **移除全部 image-slot（角色立绘 / 用户头像占位槽）** —— PRD 未定义头像字段。涉及
   对话页头部、新会话页角色卡与身份条、角色卡页列表与编辑器、身份页共六处。
2. 导航从 nav state 切换改为 react-router 真实 URL。
3. `style-hover` / `style-focus` 是原型私有属性，浏览器不认，已转为 `globals.css`
   里真实的 `:hover` / `:focus` 规则；一次性布局仍用内联 style 逐字照搬设计稿。
4. 字体改用 `@fontsource` 的简中与拉丁子集，不搬设计稿里 202 个 woff2 分片。
5. 示例数据从组件内搬到 `services/mock/seed.ts`。
6. 设计期开关 `chatStyle`（沉浸叙事 / 经典气泡）与 `showRagHints` 变成 store 里的
   UI 偏好，默认值同设计稿，两种聊天版式均已实现，未额外造设置界面。

## 已落实的 PRD 约束

- 助手回复以有序 `action` / `dialogue` 内容块渲染：动作浅色斜体独立成行，台词正常
  字体独立成行；SSE 每次追加一个已经完整校验的内容块，块内不逐字更新，历史查看与
  数据归档继续复用同一结构（验收 19）。
- 会话未绑定世界书时，界面提示切为「未绑定世界书（不检索、不注入）」，且不展示任何
  世界书命中提示（验收 20、架构约束 2）。
- API Key 只出站不回显：`ModelEndpointConfig` 无明文字段，界面仅显示「已配置 · ••••尾号」。
- 连接测试通过前禁止保存配置；Embedding 端点或模型变更后弹出重建索引提示。
- 长期记忆按角色隔离；每累计 10 轮出现记忆整理分隔线，侧边栏轮次计数同步归位。
