"""成员站点可用性判定的单点收口（docs/design/member-management.md §3.6 末）。

与库可见性（services.library.access）同构的三件套第二应用：判定只在
这里做，消费面（交互式搜索的站点枚举）不自行拼条件。

命名说明：站点客户端连接管理叫 ``site_access``（进程级共享会话），
本模块管的是"**谁**能用哪些站"，取名 visibility 与之区分。

约定：
- 返回 ``None`` 表示**不受限**（超管、PAT/Agent、all_sites 成员）；
- 站点白名单只作用于**成员发起的交互式搜索**；订阅链路的被动匹配与
  缺口搜索是系统行为，不经过本判定（资源共享原则：下载成果全家共享，
  不因发起人的站点受限而缩小搜索面）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.services.auth import Principal
from movieclaw_db.repositories.member_repo import MemberRepository


async def usable_site_ids(session: AsyncSession, principal: Principal) -> set[str] | None:
    """请求主体可用的站点 id 集合；None = 不受限（管理员语义）。"""
    if principal.is_admin or principal.member is None:
        return None
    member = principal.member
    if member.all_sites:
        return None
    return set(await MemberRepository(session).get_site_ids(member.id))
