"""阶段 8.5–8.7 结构化日志筛选、下载、删除与真实 API 测试。"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import Settings
from app.db.database import (
    create_database_engine,
    create_session_factory,
    get_db_session,
)
from app.main import create_app
from fastapi.testclient import TestClient

from scripts.init_db import initialize_database


def _flush_application_logs() -> None:
    """刷新应用文件 Handler，保证测试读取到完整日志行。"""

    for handler in logging.getLogger("app").handlers:
        handler.flush()


def test_logs_api_filters_downloads_and_partially_deletes_on_windows(
    tmp_path: Path,
) -> None:
    """查询、下载和删除复用同一筛选口径，删除后当前 Handler 仍可写入。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    application = create_app(settings)

    def override_session():
        """为每个测试请求提供隔离数据库 Session。"""

        with session_factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    logger = logging.getLogger("app.test.logs")
    secret = "sk-log-api-secret-12345678"
    logger.debug(
        "调试记录",
        extra={"event": "debug_candidate", "business_ids": {"taskId": "task-1"}},
    )
    logger.warning(
        "调用失败 api_key=%s",
        secret,
        extra={"event": "security_warning", "business_ids": {}},
    )
    logger.error(
        "上游服务失败",
        extra={"event": "recent_error", "business_ids": {"sessionId": "session-1"}},
    )
    _flush_application_logs()

    old_time = datetime.now(UTC) - timedelta(days=8)
    old_record = {
        "timestamp": old_time.isoformat().replace("+00:00", "Z"),
        "level": "INFO",
        "module": "app.test.old",
        "event": "old_event",
        "requestId": "-",
        "message": "八天前的记录",
        "businessIds": {},
    }
    rotated_path = settings.logs_dir / "loreweave.log.1"
    rotated_path.write_text(
        json.dumps(old_record, ensure_ascii=False, separators=(",", ":"))
        + "\nmalformed api_key=raw-secret-must-not-leak\n",
        encoding="utf-8",
    )
    old_epoch = old_time.timestamp()
    os.utime(rotated_path, (old_epoch, old_epoch))

    try:
        with TestClient(application) as client:
            health_response = client.get("/api/health")
            health_request_id = health_response.headers["X-Request-ID"]
            assert health_request_id.startswith("req_")
            _flush_application_logs()
            request_entries = client.get("/api/logs?level=INFO&range=1d").json()["data"]
            assert all(
                entry["event"] != "http_request_completed" for entry in request_entries
            )

            error_response = client.get("/api/logs?level=ERROR&range=1d")
            assert error_response.status_code == 200
            error_entries = error_response.json()["data"]
            assert [entry["event"] for entry in error_entries] == ["recent_error"]
            assert error_entries[0]["businessIds"] == {"sessionId": "session-1"}

            recent_response = client.get("/api/logs?range=7d")
            assert recent_response.status_code == 200
            recent_events = {entry["event"] for entry in recent_response.json()["data"]}
            assert "old_event" not in recent_events
            assert "log_parse_failed" not in recent_events

            download_response = client.get("/api/logs/download?level=WARNING&range=1d")
            assert download_response.status_code == 200
            assert (
                download_response.headers["content-type"] == "text/plain; charset=utf-8"
            )
            assert "attachment" in download_response.headers["content-disposition"]
            assert secret not in download_response.text
            downloaded = [
                json.loads(line) for line in download_response.text.splitlines()
            ]
            assert [entry["event"] for entry in downloaded] == ["security_warning"]

            malformed_response = client.get("/api/logs?range=all")
            all_entries = malformed_response.json()["data"]
            assert {"DEBUG", "INFO", "WARNING", "ERROR"} <= {
                entry["level"] for entry in all_entries
            }
            all_events = [entry["event"] for entry in all_entries]
            assert all_events.index("recent_error") < all_events.index("old_event")
            malformed_entries = [
                entry for entry in all_entries if entry["event"] == "log_parse_failed"
            ]
            assert len(malformed_entries) == 1
            assert malformed_entries[0]["message"] == "日志记录格式损坏"
            assert "raw-secret-must-not-leak" not in malformed_response.text

            delete_response = client.delete("/api/logs?level=DEBUG&range=all")
            assert delete_response.status_code == 200
            assert delete_response.json()["data"] == {"count": 1}

            logger.info("删除后继续写入", extra={"event": "after_delete"})
            _flush_application_logs()
            assert client.get("/api/logs?level=DEBUG").json()["data"] == []
            info_events = {
                entry["event"]
                for entry in client.get("/api/logs?level=INFO").json()["data"]
            }
            assert {"logs_deleted", "after_delete"} <= info_events

            assert client.get("/api/logs?level=TRACE").status_code == 422
            assert client.get("/api/logs?range=2d").status_code == 422
    finally:
        engine.dispose()
