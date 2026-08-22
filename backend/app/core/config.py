"""读取后端非敏感配置，并统一解析项目路径。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """阶段 0 所需的最小应用配置。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="LOREWEAVE_",
        extra="ignore",
    )

    app_name: str = "织境 LoreWeave API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    frontend_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    data_dir: Path = Path("data")
    # 角色回复的输出上限。不下发时由上游取模型默认值，长回复会在 JSON 中途被截断。
    chat_max_tokens: int = 8192

    @property
    def cors_origins(self) -> list[str]:
        """把逗号分隔的本地前端来源转换为 CORS 列表。"""

        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    @property
    def resolved_data_dir(self) -> Path:
        """相对数据目录始终以项目根目录为基准。"""

        if self.data_dir.is_absolute():
            return self.data_dir
        return (PROJECT_ROOT / self.data_dir).resolve()

    @property
    def database_path(self) -> Path:
        """返回 SQLite 事实数据库的统一路径。"""

        return self.resolved_data_dir / "app.db"

    @property
    def chroma_dir(self) -> Path:
        """返回 Chroma 可重建索引的持久化目录。"""

        return self.resolved_data_dir / "chroma"

    @property
    def logs_dir(self) -> Path:
        """返回 UTF-8 滚动日志目录。"""

        return self.resolved_data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        """返回有时效的临时业务导出目录。"""

        return self.resolved_data_dir / "exports"

    @property
    def database_url(self) -> str:
        """生成 SQLAlchemy 使用的 SQLite URL。"""

        return f"sqlite+pysqlite:///{self.database_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    """缓存已校验配置，避免每次请求重复读取环境。"""

    return Settings()
