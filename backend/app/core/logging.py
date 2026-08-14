"""UTF-8 JSON Lines 滚动日志、请求上下文和敏感信息过滤。"""

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock

from app.core.config import Settings, get_settings

LOG_FILE_NAME = "loreweave.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
REDACTED = "[REDACTED]"
DEFAULT_EVENT = "application_log"

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

_request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
_handler_lock = RLock()


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


def bind_request_id(request_id: str) -> Token[str]:
    """把请求标识绑定到当前异步上下文，供同一请求链路自动取用。"""

    return _request_id_context.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """请求结束后恢复进入请求前的日志上下文。"""

    _request_id_context.reset(token)


class SensitiveDataFilter(logging.Filter):
    """在记录进入应用 Handler 前脱敏并补齐结构化字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_log_text(record.getMessage())
        record.args = ()
        record.request_id = sanitize_log_text(
            getattr(record, "request_id", _request_id_context.get())
        )
        record.event = sanitize_log_text(getattr(record, "event", DEFAULT_EVENT))
        raw_business_ids = getattr(record, "business_ids", {})
        if isinstance(raw_business_ids, dict):
            record.business_ids = {
                sanitize_log_text(key): sanitize_log_text(value)
                for key, value in raw_business_ids.items()
            }
        else:
            record.business_ids = {}
        return True


class StructuredLogFormatter(logging.Formatter):
    """把每条记录输出为单行 JSON，并避免写入原始异常详情。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "module": record.name,
            "event": record.event,
            "requestId": record.request_id,
            "message": sanitize_log_text(record.getMessage()),
            "businessIds": record.business_ids,
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exceptionType"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _owned_handler(application_logger: logging.Logger, log_path: Path):
    """查找当前日志路径对应的应用文件 Handler。"""

    return next(
        (
            handler
            for handler in application_logger.handlers
            if getattr(handler, "loreweave_log_path", None) == log_path
        ),
        None,
    )


def configure_logging(settings: Settings | None = None) -> Path:
    """幂等配置 `app` Logger 的 UTF-8 JSON Lines 滚动文件 Handler。"""

    resolved_settings = settings or get_settings()
    resolved_settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = (resolved_settings.logs_dir / LOG_FILE_NAME).resolve()
    application_logger = logging.getLogger("app")
    application_logger.setLevel(logging.DEBUG)
    application_logger.disabled = False
    application_logger.propagate = False
    handler_level = logging.INFO if resolved_settings.environment == "production" else logging.DEBUG

    with _handler_lock:
        # Alembic 的日志配置可能禁用导入较早的模块 Logger，应用配置时统一恢复。
        for logger_name, candidate in logging.Logger.manager.loggerDict.items():
            if logger_name.startswith("app.") and isinstance(candidate, logging.Logger):
                candidate.disabled = False

        existing = _owned_handler(application_logger, log_path)
        if (
            existing is not None
            and existing.level == handler_level
            and isinstance(existing.formatter, StructuredLogFormatter)
        ):
            return log_path

        for handler in list(application_logger.handlers):
            if getattr(handler, "loreweave_log_path", None) is None:
                continue
            application_logger.removeHandler(handler)
            handler.flush()
            handler.close()

        handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.loreweave_log_path = log_path
        handler.setLevel(handler_level)
        handler.addFilter(SensitiveDataFilter())
        handler.setFormatter(StructuredLogFormatter())
        application_logger.addHandler(handler)
    return log_path


@contextmanager
def pause_application_file_logging(settings: Settings) -> Iterator[None]:
    """在全局日志锁内关闭当前文件 Handler，供 Windows 安全替换日志文件。"""

    log_path = (settings.logs_dir / LOG_FILE_NAME).resolve()
    application_logger = logging.getLogger("app")
    with _handler_lock:
        handler = _owned_handler(application_logger, log_path)
        if handler is not None:
            application_logger.removeHandler(handler)
            handler.flush()
            handler.close()
        try:
            yield
        finally:
            configure_logging(settings)
