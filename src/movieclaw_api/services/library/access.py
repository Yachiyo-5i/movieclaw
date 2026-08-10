"""库访问判定的单点收口（docs/design/member-management.md §3.6）。

全系统只有这里回答"这个主体能不能看见这个库"。消费面（库列表/详情/条目、
全局搜索、Jellyfin 视图、缩略图）一律调用本模块，不自行拼查询条件——
未来访问规则演进（权限等级/私人库）只改这一个文件。

约定：
- 返回 ``None`` 表示**不受限**（超管、PAT/Agent、all_libraries 成员）——
  与"空集合 = 什么都看不见"严格区分；
- 不可见按 404 处理（不泄露"存在但你不能看"）；
- Jellyfin 侧没有 Principal，用 ``member_visible_ids``（按 member_id 哨兵
  约定：0=超管，不受限）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.auth import Principal
from movieclaw_db.repositories.member_repo import MemberRepository


async def member_visible_ids(session: AsyncSession, member_id: int) -> set[int] | None:
    """按成员 id（0=超管）返回可见库 id 集合；None = 不受限。

    成员行不存在（被删除后残留的凭据竞态）按"什么都看不见"处理。
    """
    if member_id == 0:
        return None
    repo = MemberRepository(session)
    member = await repo.get(member_id)
    if member is None:
        return set()
    if member.all_libraries:
        return None
    return set(await repo.get_library_ids(member_id))


async def visible_library_ids(session: AsyncSession, principal: Principal) -> set[int] | None:
    """请求主体的可见库 id 集合；None = 不受限（管理员语义）。"""
    if principal.is_admin or principal.member is None:
        return None
    member = principal.member
    if member.all_libraries:
        return None
    return set(await MemberRepository(session).get_library_ids(member.id))


async def assert_library_visible(
    session: AsyncSession, principal: Principal, library_id: int
) -> None:
    """断言主体可见该库；不可见抛 404（与"库不存在"不可区分）。"""
    visible = await visible_library_ids(session, principal)
    if visible is not None and library_id not in visible:
        raise NotFoundException(f"媒体库不存在：id={library_id}")
