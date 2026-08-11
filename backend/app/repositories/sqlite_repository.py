"""不私自提交事务的 SQLite 通用数据访问层。"""

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.db.models import Base

ModelType = TypeVar("ModelType", bound=Base)


class SQLiteRepository:
    """封装阶段 1 所需的基础增删查与 flush，提交由服务层负责。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, entity: ModelType) -> ModelType:
        """将实体加入当前事务。"""

        self.session.add(entity)
        return entity

    def get(self, model: type[ModelType], identifier: Any) -> ModelType | None:
        """按主键读取实体，支持复合主键标识。"""

        return self.session.get(model, identifier)

    def list_all(
        self,
        model: type[ModelType],
        *,
        order_by: Sequence[ColumnElement[Any]] = (),
    ) -> list[ModelType]:
        """按明确顺序一次返回 MVP 列表数据。"""

        statement = select(model).order_by(*order_by)
        return list(self.session.scalars(statement).all())

    def delete(self, entity: Base) -> None:
        """在当前事务标记删除，不在 Repository 内提交。"""

        self.session.delete(entity)

    def flush(self) -> None:
        """把当前事务变更发送到 SQLite，以便尽早发现约束错误。"""

        self.session.flush()
