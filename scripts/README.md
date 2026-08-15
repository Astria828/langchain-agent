# 项目脚本

`start.ps1` 是本地一键启动入口。它会先调用 `init_db.py`，再在当前 PowerShell 窗口中托管 FastAPI 与 Vite，健康检查通过后自动打开浏览器。按 `Ctrl+C` 或关闭窗口会停止两个子进程：

```powershell
.\scripts\start.ps1
```

项目根目录的 `start.cmd` 是供 Windows 双击使用的薄入口，不包含额外启动逻辑。

阶段 1 提供 `init_db.py`，用于创建运行目录、将 SQLite 升级到最新 Alembic revision，并幂等创建两个 Chroma Collection：

```powershell
.\.venv\Scripts\python.exe .\scripts\init_db.py
```

固定 Collection 名称为 `worldbook_entries` 与 `long_term_memories`，两类向量物理隔离。

阶段 7 提供失败长期记忆任务的本地查看和重试入口。先列出任务 ID：

执行任务管理命令前建议先停止后端进程，避免后台调度器同时领取刚恢复的任务。

```powershell
.\.venv\Scripts\python.exe .\scripts\manage_tasks.py list-failed-memory
```

再把指定 `failed` 任务恢复为 `pending`，并使用独立数据库 Session 立即执行一次：

```powershell
.\.venv\Scripts\python.exe .\scripts\manage_tasks.py retry-memory <taskId>
```

只有 `failed` 的 `memory_consolidation` 任务可通过该命令重试。命令只输出任务 ID、会话 ID、目标轮次和脱敏错误。成功时退出码为 `0`；任务不存在、状态不允许或数据库未初始化时为 `1`；执行后仍失败时为 `2`。
