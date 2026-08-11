"""UTF-8 滚动日志配置和敏感信息过滤。"""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import Settings, get_settings

LOG_FILE_NAME = "loreweave.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
REDACTED = "[REDACTED]"

JSON_SECRET_PATTERN = re.compile(
    r"(?i)([\"'](?:api[_-]?key|authorization|secret(?:_ref)?|access[_-]?token)"
    r"[\"']\s*:\s*[\"'])([^\"']+)([\"'])"
)
KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|secret(?:_ref)?|access[_-]?token)"
    r"(\s*[=:]\s*|\s+)(?:Bearer\s+)?([^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def sanitize_log_text(value: object) -> str:
    """脱敏常见密钥字段、Bearer 凭据和 OpenAI 风格 Key。"""

    sanitized = str(value)
    sanitized = JSON_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}",
        sanitized,
    )
    sanitized = KEY_VALUE_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED}",
        sanitized,
    )
    sanitized = BEARER_PATTERN.sub(f"Bearer {REDACTED}", sanitized)
    return OPENAI_KEY_PATTERN.sub(REDACTED, sanitized)


class SensitiveDataFilter(logging.Filter):
    """在记录进入任意应用 Handler 前脱敏并补齐 request_id。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_log_text(record.getMessage())
        record.args = ()
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class RedactingFormatter(logging.Formatter):
    """对异常文本在内的最终日志行再执行一次脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(super().format(record))


def configure_logging(settings: Settings | None = None) -> Path:
    """幂等配置 `app` Logger 的 UTF-8 滚动文件 Handler。"""

    resolved_settings = settings or get_settings()
    resolved_settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = (resolved_settings.logs_dir / LOG_FILE_NAME).resolve()
    application_logger = logging.getLogger("app")
    application_logger.setLevel(logging.DEBUG)
    application_logger.propagate = False

    for handler in list(application_logger.handlers):
        owned_path = getattr(handler, "loreweave_log_path", None)
        if owned_path == log_path:
            return log_path
        if owned_path is not None:
            application_logger.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.loreweave_log_path = log_path
    handler.addFilter(SensitiveDataFilter())
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
        )
    )
    application_logger.addHandler(handler)
    return log_path
