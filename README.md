# 织境 LoreWeave

织境是一个单用户、单 Agent、本地优先的角色扮演应用。当前 MVP 已可完整本地运行，前端使用 React + Vite，后端使用 FastAPI，SQLite 是业务事实源，Chroma 保存可重建的世界书与长期记忆向量索引。

## 当前已实现

- 双模型配置：分别配置主模型和 Embedding 模型，兼容 OpenAI-compatible 接口；API Key 通过 Windows DPAPI 加密保存。
- 角色内容管理：用户身份、角色卡、对话示例、世界书、常驻/关键词/向量条目及会话创建快照。
- 角色对话：SSE 流式生成，按动作、场景和台词内容块展示，使用最近 20 轮短期上下文。
- 对话操作：最后一条完整角色回复支持“重说”“继续说”和“删除本轮”；删除是物理删除，重说直接覆盖原回复，继续说追加到原回复。
- 推荐回复：根据身份、角色和最近对话生成一句用户侧草稿，只填入发送框，不自动发送，可继续修改。
- 记忆系统：世界书与长期记忆双 RAG，每累计 10 个稳定轮次执行一次长期记忆整理。
- 历史与数据：会话归档、长期记忆查看、单会话 JSON 导出、全量业务 ZIP 导出和本地数据备份。
- 系统日志：UTF-8 结构化滚动日志，支持分级筛选、下载和删除；普通成功请求不写日志，只保留回复生成、任务完成等业务事件以及告警和错误。
- 本地启动：Windows 下可双击 `start.cmd`，自动升级数据库、启动前后端并打开浏览器。

详细约束见 `doc/`：

- `PRD.md`：需求范围、对话增量功能和验收标准；
- `API接口契约.md`：REST、SSE、DTO 和错误响应；
- `数据模型与存储规范.md`：SQLite、Chroma 和快照规则；
- `阶段9验收清单.md`：测试证据和最终验收边界。

## 1. 环境要求

- Windows PowerShell：`pwsh`
- Python：3.11–3.14
- Node.js 与 npm：支持 Vite 5

所有文本、环境文件和导出内容均使用 UTF-8。API Key 通过 Windows DPAPI 加密后保存在 SQLite，不写入 `.env`。

## 2. 首次安装

在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
Copy-Item -LiteralPath '.env.example' -Destination '.env'
.\.venv\Scripts\python.exe .\scripts\init_db.py

Set-Location -LiteralPath '.\frontend'
npm ci
Set-Location -LiteralPath '..'
```

如果本机使用 Python 3.12、3.13 或 3.14，只需替换第一条命令的版本号。`init_db.py` 可重复执行：它会保留业务数据、升级 SQLite，并幂等初始化两个 Chroma Collection；一键启动器每次运行时也会先执行这一步。

## 3. 启动

推荐直接双击项目根目录的 `start.cmd`。启动器会依次升级数据库、启动后端和前端、等待健康检查通过，然后自动打开 `http://localhost:5173`。

运行期间保留启动器窗口即可；按 `Ctrl+C` 或关闭该窗口会同时停止前后端。也可以在项目根目录通过 PowerShell 启动：

```powershell
.\scripts\start.ps1
```

需要分别调试进程时，仍可手动启动。终端一：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

终端二：

```powershell
Set-Location -LiteralPath '.\frontend'
npm run dev
```

打开 `http://localhost:5173`。健康检查和 API 文档：

- `http://127.0.0.1:8000/api/health`
- `http://127.0.0.1:8000/docs`

前端九个页面固定访问真实 REST/SSE，不再需要 Mock 开关。

## 4. 首次使用顺序

1. 在“API 配置”分别测试并保存主模型与 Embedding 模型。
2. 保存用户身份，创建角色卡和世界书。
3. 如 Embedding 配置变更后出现“需要重建”，先在 API 配置页执行索引重建。
4. 新建会话时选择世界书，或选择“不绑定世界书”。
5. 在对话页直接输入内容，或点击“推荐回复”生成可编辑的用户回复草稿。
6. 最后一条完整角色回复可以重说、继续说或物理删除本轮。
7. 每累计 10 个稳定轮次会触发一次长期记忆整理。已整理轮次仍允许操作，但不会回溯修改既有长期记忆；删除已整理轮次也不会复用其轮次编号。
8. 在“长期记忆”“数据管理”“系统日志”查看结果或导出数据。

应用使用 OpenAI-compatible 接口：Base URL 应填写到服务商的版本根路径，例如 `https://example.com/v1`，不要附带 `/chat/completions`、查询参数或密钥。

## 5. 测试与检查

后端完整测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

后端静态检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
.\.venv\Scripts\python.exe -m ruff format --check backend tests scripts
```

前端生产构建：

```powershell
Set-Location -LiteralPath '.\frontend'
npm run build
```

核心端到端测试位于 `tests/test_e2e_core_flow.py`，使用隔离 SQLite 与确定性模型替身，覆盖“双 API → 身份/角色/世界书 → 十轮 SSE → 记忆整理 → 历史 → 单会话/全量导出”，不消耗真实模型额度。

对话删除、重说、继续说、推荐回复、稳定轮次编号和数据库迁移分别由 Chat Service/API、Memory Service 与 Database 测试覆盖。

## 6. 本地数据与备份

默认运行目录为 `data/`：

- `data/app.db`：业务事实数据和 DPAPI 加密后的模型密钥；
- `data/chroma/`：可由 SQLite 重建的向量索引；
- `data/logs/`：脱敏结构化业务日志；普通页面读取和其他成功请求不写入；
- `data/exports/`：下载期间使用的临时导出文件。

建议停止后端后备份整个 `data` 目录，避免同时复制 SQLite WAL 文件：

```powershell
$backupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path -Path '.\backups' -ChildPath $backupStamp
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath '.\data' -Destination $backupRoot -Recurse
```

恢复时同样先停止后端，并保留当前目录以便回滚：

```powershell
$restoreStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Move-Item -LiteralPath '.\data' -Destination ".\data.before-restore-$restoreStamp"
Copy-Item -LiteralPath '.\backups\<备份时间>\data' -Destination '.\data' -Recurse
.\.venv\Scripts\python.exe .\scripts\init_db.py
```

DPAPI 密钥通常只可由原 Windows 用户在原机器解密。把备份恢复到其他用户或机器后，需要在“API 配置”重新输入并保存两组 API Key。Chroma 损坏或丢失时，以 SQLite 为准，在设置页执行索引重建。

## 7. 常见问题

### 前端提示无法连接后端

确认后端终端仍在运行，并执行：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -Method Get
```

### 返回 `MODEL_NOT_CONFIGURED`

进入“API 配置”，先测试连接，再保存对应配置。测试参数必须与保存参数一致。

### 返回 `INDEX_REBUILD_REQUIRED`

Embedding 模型、Base URL 或向量维度发生变化。完成设置页的索引重建后再继续对话。

### Embedding 服务临时失败

普通对话会保留已提交消息，并降级为常驻/关键词世界书检索或空向量上下文；世界书条目和记忆的失败索引会保留状态，恢复服务后执行重嵌入或全量重建。

### 长期记忆任务失败

建议先停止后端，查看并重试指定任务：

```powershell
.\.venv\Scripts\python.exe .\scripts\manage_tasks.py list-failed-memory
.\.venv\Scripts\python.exe .\scripts\manage_tasks.py retry-memory <taskId>
```

### API 请求失败但原因不明确

错误响应头和响应体包含 `requestId`。在“系统日志”按时间筛选，并用该 ID 定位同一请求；日志不会记录 API Key、完整 Prompt 或完整对话正文。

### 切换页面是否会记录系统日志

不会。普通成功 HTTP 请求不写系统日志；INFO 只保留回复生成、配置保存、记忆整理、索引重建、向量清理和导出完成等业务事件。WARNING 与 ERROR 用于可恢复问题和失败事件。

## 8. 当前验收边界

自动化测试覆盖 PRD 核心标准及后续新增对话能力的业务规则、接口、持久化、迁移、异常与安全边界。真实模型是否长期遵循角色 Prompt、推荐回复是否符合具体角色语气，以及不同屏幕下的最终视觉效果，仍需使用用户自己的模型配置进行人工冒烟；这些效果无法由仓库内的确定性模型替身完全验证。

项目不提供 Docker Compose：当前 Windows 本地应用只依赖 Python、Node.js 和本地数据目录，直接安装路径更短，也避免额外维护一套未使用的容器配置。
