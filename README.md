# 织境 LoreWeave

织境是一个单用户、单 Agent、本地优先的角色扮演应用。当前已完成阶段 0–4：模型配置、用户身份、角色卡和世界书使用真实 FastAPI、SQLite 与 Chroma，其余业务页面继续保留 Mock 体验。

详细需求和约束位于 `doc/`：

- `PRD.md`：产品范围与验收标准；
- `API接口契约.md`：REST、SSE、DTO 和错误响应；
- `数据模型与存储规范.md`：SQLite、Chroma 和快照规则；
- `开发计划书.md`：阶段划分与完成标准。

## 1. 环境要求

- Windows PowerShell：`pwsh`
- Python：3.11–3.14
- Node.js：满足 `frontend/package.json` 中 Vite 5 的要求
- npm

所有文本和环境文件使用 UTF-8。

## 2. 初始化后端

在项目根目录执行：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
Copy-Item -LiteralPath '.env.example' -Destination '.env'
```

如果本机没有 Python 3.14，可将第一条命令改为已安装的 3.11、3.12 或 3.13。

## 3. 启动后端

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

验证健康检查：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -Method Get
```

API 文档：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

## 4. 启动前端

```powershell
Set-Location -LiteralPath '.\frontend'
D:\node\npm.cmd install
D:\node\npm.cmd run dev
```

打开 `http://localhost:5173`。默认阶段式模式下，API 配置、索引重建、用户身份、角色卡、世界书、会话 CRUD/历史消息和实时基础对话均访问真实后端；记忆、数据与日志仍保留 Mock 边界。需要将全部页面切换到真实后端时，在 `frontend/.env` 中设置：

```dotenv
VITE_USE_MOCK=false
```

Vite 会将 `/api/` 请求代理到 `http://localhost:8000`。

当前阶段可以真实读取、测试并保存模型配置，执行全局索引重建，持久化用户身份与多个角色卡，完成世界书原文、草稿、正式条目和向量索引闭环，并使用真实角色卡与可选世界书创建、切换、改名和删除会话。会话创建会冻结身份、角色卡和已启用世界书条目，并保存可用的角色开场白。基础 Chat Chain 会注入应用规则、身份快照、角色卡快照、最近 20 轮和当前输入，通过 SSE 返回并保存有序的 `action` / `dialogue` 内容块。

## 5. 测试与检查

后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -c backend\pyproject.toml tests
```

后端静态检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
.\.venv\Scripts\python.exe -m ruff format --check backend tests scripts
```

前端类型检查与构建：

```powershell
Set-Location -LiteralPath '.\frontend'
D:\node\npm.cmd run build
```

## 6. 当前阶段边界

阶段 0 已提供 FastAPI 入口、CORS、`request_id`、统一异常、健康检查、环境模板和基础测试。阶段 1 已提供 SQLAlchemy 数据模型、Pydantic DTO、Alembic 初始迁移、SQLite/Chroma Repository、双 Chroma Collection、Windows DPAPI 密钥保护、UTF-8 滚动日志和初始化脚本。阶段 2 已提供两组模型配置的安全读取、真实连接测试、测试后保存、Embedding 版本状态、带唯一任务与进度的双 Collection 全量重建、中断任务启动恢复，以及 API 配置页面的真实前后端联调。阶段 3 已提供用户身份与角色卡请求 DTO、内容服务、CRUD API、历史会话删除保护、前端真实数据接入和对应测试。阶段 4 已提供世界书与条目 CRUD、LangChain 结构化拆分、临时草稿确认、SQLite 先提交再生成 Embedding、按世界书 metadata 写入 Chroma、索引过期重建、确定 ID 向量清理、失败补偿任务、完整 REST API 和前端真实数据接入。阶段 5 已提供会话不可变快照、历史消息、基础 Chat Chain、结构化回复降级、消息事务和真实 SSE 前后端联调。阶段 6 已提供“当前输入 + 最近 4 轮完整对话”的统一检索文本、世界书混合召回、角色级跨会话长期记忆召回、单次共享 Embedding、固定 Prompt 注入顺序、`retrieval` SSE 与命中记录持久化，以及索引待重建阻断和检索故障降级。阶段 7 已完成五类记忆提取、六种整合动作、十轮幂等任务、来源追溯、SQLite 与 Chroma 一致性、失败安全重试、向量清理补偿、同会话积压串行执行、启动恢复、真实查询与幂等失效 API，以及按角色、类型、状态筛选的真实记忆页面；分层测试覆盖第 9/10/20/30 轮触发、跨会话召回和角色隔离。

初始化或升级 SQLite：

```powershell
.\.venv\Scripts\python.exe .\scripts\init_db.py
```

后续阶段尚未实现：

- 数据导出、日志管理及后续阶段能力。
