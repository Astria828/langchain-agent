# 项目脚本

阶段 1 提供 `init_db.py`，用于创建运行目录、将 SQLite 升级到最新 Alembic revision，并幂等创建两个 Chroma Collection：

```powershell
.\.venv\Scripts\python.exe .\scripts\init_db.py
```

固定 Collection 名称为 `worldbook_entries` 与 `long_term_memories`，两类向量物理隔离。
