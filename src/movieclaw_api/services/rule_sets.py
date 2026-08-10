"""规则组服务：默认组懒种子、CRUD 与"被引用禁删"语义。

规则组是纯参数包（movieclaw_matcher.RuleSetSpec 定 schema），本服务只负责
校验与持久化；判断逻辑在匹配内核。修改规则组只影响之后的评估、不追溯已
grabbed 的工单——这一条不需要任何机制，天然成立。
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import (
    BadRequestException,
    ConflictException,
    NotFoundException,
)
from movieclaw_db.models import RuleSet
from movieclaw_db.repositories import RuleSetRepository
from movieclaw_matcher import RuleSetSpec

logger = logging.getLogger("movieclaw_api.rule_sets")

_DEFAULT_NAME = "默认规则组"

# 懒种子默认组的安全预设：只收 1080p 及以上（顺序即偏好）、排除零做种死种。
# 此前是"全不限"（spec={}），新用户第一批抓到的可能是低清枪版或没人做种的
# 死种——对家用场景太危险。只影响首次创建；已有部署的默认组（无论是否被
# 用户改过）一律不动。
_DEFAULT_SPEC = {"resolutions": ["2160p", "1080p"], "min_seeders": 1}


class RuleSetService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = RuleSetRepository(session)

    async def ensure_default(self) -> RuleSet:
        """取默认规则组，不存在则懒种子一个安全预设的（幂等）。

        放在服务层而非迁移里做 seed：迁移保持纯 DDL，且名字/形态想改时
        不用动历史迁移。
        """
        existing = await self._repo.get_default()
        if existing is not None:
            return existing
        row = await self._repo.save(
            RuleSet(name=_DEFAULT_NAME, is_default=True, spec=dict(_DEFAULT_SPEC))
        )
        logger.info("已创建默认规则组（1080p 及以上、做种数 ≥1），新订阅未指定规则组时使用它")
        return row

    async def list_all(self) -> list[RuleSet]:
        await self.ensure_default()
        return await self._repo.list_all()

    async def reference_counts(self) -> dict[int, int]:
        """{rule_set_id: 引用它的订阅数}，未被引用的组不在结果里。"""
        return await self._repo.reference_counts()

    async def count_references(self, rule_set_id: int) -> int:
        return await self._repo.count_references(rule_set_id)

    async def get(self, rule_set_id: int) -> RuleSet:
        row = await self._repo.get(rule_set_id)
        if row is None:
            raise NotFoundException(f"规则组不存在：#{rule_set_id}")
        return row

    async def create(self, name: str, spec: dict) -> RuleSet:
        cleaned = self._validate(name, spec)
        try:
            return await self._repo.save(RuleSet(name=name.strip(), spec=cleaned))
        except IntegrityError as exc:
            # 名称撞唯一约束：给可读中文错误，而不是让 500 裸奔到前端
            await self._repo.rollback()
            raise ConflictException(f"规则组「{name.strip()}」已存在，请换一个名称") from exc

    async def update(self, rule_set_id: int, *, name: str, spec: dict) -> RuleSet:
        row = await self.get(rule_set_id)
        # rollback 会使 ORM 对象过期，异常分支不能再读 row.name——先落到局部变量
        new_name = name.strip() or row.name
        row.name = new_name
        row.spec = self._validate(new_name, spec)
        try:
            return await self._repo.save(row)
        except IntegrityError as exc:
            await self._repo.rollback()
            raise ConflictException(f"规则组「{new_name}」已存在，请换一个名称") from exc

    async def set_default(self, rule_set_id: int) -> RuleSet:
        """把默认标记转移到指定组：新订阅未指定规则组时用它。幂等。

        只换"谁是默认"，不改任何订阅的挂靠——已有订阅继续用各自的组。
        """
        row = await self.get(rule_set_id)
        if row.is_default:
            return row
        row = await self._repo.set_default(row)
        logger.info("默认规则组已切换为「%s」", row.name)
        return row

    async def delete(self, rule_set_id: int) -> None:
        """删除规则组。默认组与被订阅引用的组禁删（显式报错优于隐式改挂靠）。"""
        row = await self.get(rule_set_id)
        if row.is_default:
            raise BadRequestException("默认规则组不可删除")
        references = await self._repo.count_references(rule_set_id)
        if references > 0:
            raise ConflictException(
                f"规则组「{row.name}」正被 {references} 个订阅引用，"
                "请先把这些订阅改到其他规则组再删除"
            )
        await self._repo.delete(row)
        logger.info("规则组「%s」已删除", row.name)

    @staticmethod
    def _validate(name: str, spec: dict) -> dict:
        """经 RuleSetSpec 校验并规整（未知字段忽略、类型收敛），存精简形态。

        未知字段被静默忽略是 pydantic 默认行为，也是刻意保留的兼容策略：
        新版本加字段（如 dv）后回退旧版本，旧代码读到新字段不会报错。
        """
        if not name.strip():
            raise BadRequestException("规则组名称不能为空")
        try:
            parsed = RuleSetSpec.model_validate(spec)
        except ValueError as exc:
            raise BadRequestException(f"规则组参数不合法：{exc}") from exc
        return parsed.model_dump(exclude_defaults=True, mode="json")
