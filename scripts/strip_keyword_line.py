"""清理世界书条目正文里与 keywords 字段重复的「触发关键词：」行。

该行来自世界书原文，拆分时信息已完整提取进 keywords 字段，继续留在正文里
只会和界面下方的关键词框重复展示。剥离后条目的索引文本发生变化，因此这些
条目会被标记为 stale，需要在世界书页面点一次「重新生成 Embedding」。

召回不受影响：
- 关键词匹配只读 keywords 字段，与正文无关；
- 向量索引文本本就有独立的「关键词：」行，全部关键词仍在其中。

用法（先看结果，确认无误再写库）：
    .venv/Scripts/python.exe scripts/strip_keyword_line.py            # 预演，不改库
    .venv/Scripts/python.exe scripts/strip_keyword_line.py --apply    # 实际写入
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.repositories.chroma_repository import (  # noqa: E402
    build_worldbook_index_text,
    calculate_content_hash,
)
from app.services.worldbook_service import strip_duplicate_keyword_line  # noqa: E402

DB_PATH = ROOT / "data" / "app.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写入数据库，默认只预演")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite 路径")
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "select id, name, category, keywords_json, content, content_hash"
        " from world_book_entries"
    ).fetchall()

    planned: list[tuple[str, str, str, str]] = []
    for row in rows:
        keywords = json.loads(row["keywords_json"])
        stripped = strip_duplicate_keyword_line(row["content"], keywords)
        if stripped == row["content"]:
            continue
        new_hash = calculate_content_hash(
            build_worldbook_index_text(
                name=row["name"],
                category=row["category"],
                keywords=keywords,
                content=stripped,
            )
        )
        planned.append((row["id"], row["name"], stripped, new_hash))

    print(f"条目总数 {len(rows)}，需要清理 {len(planned)} 条")
    for _, name, stripped, _ in planned[:5]:
        print(f"  · {name} → 正文首行改为：{stripped.splitlines()[0][:40]}…")
    if len(planned) > 5:
        print(f"  · …其余 {len(planned) - 5} 条同理")

    if not args.apply:
        print("\n这是预演，数据库未改动。确认无误后加 --apply 重新执行。")
        return 0

    # 正文与哈希在同一事务内更新，并统一标记为 stale 等待重建索引
    with connection:
        connection.executemany(
            "update world_book_entries"
            " set content = ?, content_hash = ?, index_status = 'stale',"
            "     last_index_error = null"
            " where id = ?",
            [(stripped, new_hash, entry_id) for entry_id, _, stripped, new_hash in planned],
        )
    print(f"\n已更新 {len(planned)} 条，并标记为待重建索引。")
    print("请到世界书页面点一次「重新生成 Embedding」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
