"""阶段 1B 的 Windows DPAPI 与日志脱敏测试。"""

import logging
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.logging import REDACTED, configure_logging, sanitize_log_text
from app.core.security import api_key_tail, protect_api_key, unprotect_api_key
from app.db.database import create_database_engine
from sqlalchemy import text

from scripts.init_db import initialize_database


def test_dpapi_round_trip_never_stores_plaintext() -> None:
    """密钥引用是当前 Windows 用户可解开的 DPAPI 密文。"""

    api_key = "sk-stage1b-secret-12345678"
    secret_ref = protect_api_key(api_key)

    assert secret_ref.startswith("dpapi:")
    assert api_key not in secret_ref
    assert unprotect_api_key(secret_ref) == api_key
    assert api_key_tail(api_key) == "5678"


def test_dpapi_rejects_empty_or_unknown_references() -> None:
    """无密钥和非 DPAPI 引用不能被静默接受。"""

    with pytest.raises(ValueError):
        protect_api_key("")
    with pytest.raises(ValueError):
        unprotect_api_key("plaintext-secret")


def test_sqlite_stores_only_dpapi_ciphertext(tmp_path: Path) -> None:
    """模型配置写入后，SQLite 文件中不存在 API Key 明文。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    initialize_database(settings)
    api_key = "sk-sqlite-secret-24681357"
    secret_ref = protect_api_key(api_key)
    engine = create_database_engine(settings)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE model_configs SET secret_ref = :secret_ref, key_tail = :key_tail "
                "WHERE group_name = 'main'"
            ),
            {"secret_ref": secret_ref, "key_tail": api_key_tail(api_key)},
        )

    engine.dispose()
    assert api_key.encode("utf-8") not in settings.database_path.read_bytes()


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("apiKey=plain-secret", "plain-secret"),
        ('{"apiKey": "json-secret"}', "json-secret"),
        ("Authorization=Bearer bearer-secret", "bearer-secret"),
        ("请求 Bearer token-secret", "token-secret"),
        ("服务返回 sk-abcdefgh12345678", "sk-abcdefgh12345678"),
    ],
)
def test_log_sanitizer_removes_common_secret_shapes(raw: str, secret: str) -> None:
    """常见密钥表达形式不会进入最终日志文本。"""

    sanitized = sanitize_log_text(raw)
    assert secret not in sanitized
    assert REDACTED in sanitized


def test_rotating_log_handler_writes_utf8_and_redacts(tmp_path: Path) -> None:
    """应用日志按 UTF-8 写入指定目录，并在落盘前脱敏。"""

    settings = Settings(environment="test", data_dir=tmp_path)
    log_path = configure_logging(settings)
    logger = logging.getLogger("app.test")
    secret = "sk-logfile-secret-87654321"
    logger.warning(
        "连接失败 api_key=%s 中文信息", secret, extra={"request_id": "req_test"}
    )
    for handler in logging.getLogger("app").handlers:
        handler.flush()

    log_content = log_path.read_text(encoding="utf-8")
    assert "中文信息" in log_content
    assert "request_id=req_test" in log_content
    assert secret not in log_content
    assert REDACTED in log_content
