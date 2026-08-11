"""模型配置测试、保存和安全展示规则。"""

import json
from hashlib import sha256

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.security import api_key_tail, protect_api_key, unprotect_api_key
from app.db.models import (
    LongTermMemory,
    ModelConfig,
    SessionWorldBookEntrySnapshot,
    WorldBookEntry,
    utc_now,
)
from app.gateways.model_gateway import ModelGateway, normalize_base_url
from app.repositories.sqlite_repository import SQLiteRepository
from app.schemas.dto import (
    ConnectionTestResult,
    IndexStatus,
    ModelEndpointConfig,
    ModelEndpointPayload,
    ModelGroup,
    ModelSettings,
)


class SystemService:
    """控制模型配置测试状态与 SQLite 提交边界。"""

    def __init__(self, session: Session, gateway: ModelGateway) -> None:
        self.session = session
        self.repository = SQLiteRepository(session)
        self.gateway = gateway

    def get_model_settings(self) -> ModelSettings:
        """返回两组不含明文密钥的当前生效配置。"""

        return ModelSettings(
            main=self._public_config(self._get_config("main")),
            embed=self._public_config(self._get_config("embed")),
        )

    def get_index_status(self) -> IndexStatus:
        """读取 Embedding 配置对应的全局重建标记。"""

        return IndexStatus(rebuild_required=bool(self._get_config("embed").rebuild_required))

    async def test_model(
        self,
        group: ModelGroup,
        payload: ModelEndpointPayload,
    ) -> ConnectionTestResult:
        """实际调用当前参数，并持久化不含明文的测试凭据。"""

        config = self._get_config(group)
        base_url = self._validated_base_url(payload.base_url)
        api_key = self._resolve_api_key(config, payload.api_key)

        if group == "main":
            await self.gateway.test_main(
                base_url=base_url,
                model=payload.model,
                api_key=api_key,
            )
            tested_dimension = None
            message = "主模型连接测试成功"
        else:
            tested_dimension = await self.gateway.test_embedding(
                base_url=base_url,
                model=payload.model,
                api_key=api_key,
            )
            message = f"Embedding 连接测试成功，向量维度为 {tested_dimension}"

        config.tested_fingerprint = self._fingerprint(
            base_url=base_url,
            model=payload.model,
            api_key=api_key,
        )
        config.tested_vector_dimension = tested_dimension
        config.tested_at = utc_now()
        self.session.commit()
        return ConnectionTestResult(ok=True, message=message)

    def save_model(
        self,
        group: ModelGroup,
        payload: ModelEndpointPayload,
    ) -> ModelSettings:
        """仅保存与最近成功测试完全一致的参数。"""

        config = self._get_config(group)
        base_url = self._validated_base_url(payload.base_url)
        old_api_key = unprotect_api_key(config.secret_ref) if config.secret_ref else None
        api_key = payload.api_key or old_api_key
        if api_key is None:
            raise self._missing_api_key_error()

        fingerprint = self._fingerprint(
            base_url=base_url,
            model=payload.model,
            api_key=api_key,
        )
        if config.tested_fingerprint != fingerprint:
            raise AppError(
                status_code=409,
                code="MODEL_CONFIG_NOT_TESTED",
                message="当前模型参数尚未通过连接测试",
            )

        if group == "embed" and config.tested_vector_dimension is None:
            raise AppError(
                status_code=409,
                code="MODEL_CONFIG_NOT_TESTED",
                message="Embedding 参数尚未通过有效向量测试",
            )

        if group == "main":
            active_changed = (
                config.base_url != base_url
                or config.model_name != payload.model
                or old_api_key != api_key
            )
            if active_changed:
                config.config_version += 1
        else:
            was_configured = config.config_version > 0
            vector_changed = (
                config.base_url != base_url
                or config.model_name != payload.model
                or config.vector_dimension != config.tested_vector_dimension
            )
            if not was_configured or vector_changed:
                config.config_version += 1
            if was_configured and vector_changed:
                config.rebuild_required = 1
                self._mark_all_indexes_stale()
            config.vector_dimension = config.tested_vector_dimension

        config.base_url = base_url
        config.model_name = payload.model
        if payload.api_key:
            config.secret_ref = protect_api_key(payload.api_key)
            config.key_tail = api_key_tail(payload.api_key)
        config.updated_at = utc_now()
        self.session.commit()
        return self.get_model_settings()

    def _mark_all_indexes_stale(self) -> None:
        """Embedding 向量空间变化时，使全部可索引事实记录停止就绪。"""

        stale_values = {"index_status": "stale", "last_index_error": None}
        self.session.execute(update(WorldBookEntry).values(**stale_values))
        self.session.execute(update(SessionWorldBookEntrySnapshot).values(**stale_values))
        self.session.execute(
            update(LongTermMemory).where(LongTermMemory.status == "active").values(**stale_values)
        )

    def _get_config(self, group: ModelGroup) -> ModelConfig:
        """读取迁移保证存在的固定配置行。"""

        config = self.repository.get(ModelConfig, group)
        if config is None:
            raise RuntimeError(f"缺少模型配置记录：{group}")
        return config

    @staticmethod
    def _public_config(config: ModelConfig) -> ModelEndpointConfig:
        """将内部密钥引用转换为前端安全状态。"""

        return ModelEndpointConfig(
            base_url=config.base_url,
            model=config.model_name,
            key_set=config.secret_ref is not None,
            key_tail=config.key_tail if config.secret_ref is not None else "",
        )

    @staticmethod
    def _validated_base_url(base_url: str) -> str:
        """把地址语义错误转换为稳定的 422 响应。"""

        try:
            return normalize_base_url(base_url)
        except ValueError as exc:
            raise AppError(
                status_code=422,
                code="VALIDATION_ERROR",
                message=str(exc),
            ) from exc

    @staticmethod
    def _fingerprint(*, base_url: str, model: str, api_key: str) -> str:
        """只保存参数摘要，不在测试状态中保存 API Key。"""

        payload = json.dumps(
            {
                "baseUrl": base_url,
                "model": model,
                "apiKeyHash": sha256(api_key.encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_api_key(config: ModelConfig, submitted_api_key: str) -> str:
        """空字符串表示复用已保存密钥，而不是清空。"""

        if submitted_api_key:
            return submitted_api_key
        if config.secret_ref:
            return unprotect_api_key(config.secret_ref)
        raise SystemService._missing_api_key_error()

    @staticmethod
    def _missing_api_key_error() -> AppError:
        """构造测试和保存共用的缺失密钥错误。"""

        return AppError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="尚未配置 API Key",
        )
