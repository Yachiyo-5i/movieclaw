"""PTTime 用户资料解析回归测试。

PTTime 个人中心页的用户名、UID、等级、上传/下载/分享率/做种/下载数都集中在
顶部 ``#info_block`` 或资料区表格里；旧配置用 ``a[class*='_Name']`` 全页宽匹配，
会把页面里出现的其他用户（如评论区作者）一并拼进用户名，且 ``font.color_*``
旧样式已被站点替换为 ``#info_block`` 新统计区域，导致上传/下载/分享率取不到值。

本文件用脱敏 HTML fixture 覆盖这些字段，并显式断言结果中不会出现其他用户名。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from movieclaw_tracker import load_all_sites
from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
from movieclaw_tracker.registry import get_site_config
from movieclaw_tracker.selectors import NexusPHPSelectors

PTTIME_PROFILE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>PTT :: fixture_user - 个人中心</title>
</head>
<body>
  <table id="info_block" cellpadding="4" cellspacing="0" border="0" width="97%">
    <tr>
      <td class="bottom clearfix" align="left">
        <span class="medium left">嗨,
          <a  href="userdetails.php?id=12345" class='PowerUser_Name'><b>fixture_user</b></a><b title='发布者：0级'>🎈</b>[UID=12345][(小学)Power User][<a href="userdetails.php?id=12345" class="fcb PowerUser">个人中心</a>]
          [<a href="logout.php"><b>退出</b></a>]
          <span>[<a href="bookmarks.php?inclbookmarked=1&amp;incldead=0">收藏</a>]</span>
          <span>[<a href="myrss.php">RSS下载筐</a>]</span>
        </span>
      </td>
    </tr>
    <tr>
      <td class="bottom clearfix" align="right">
        <div class="left">
          <span class='mr5'><font class="">分享率:</font>3.14</span>
          <span class='mr5'><font class='fcg'>上传:</font>12.34TB</span>
          <span class='mr5'><font class='fcr'> 下载:</font>5.67TB</span>
          <span class='mr5'><font>活动:</font>
            <font title="当前做种" class="fcg">⬆</font>12
            <font title="当前下载" class="fcr">⬇</font>3</span>
          <span class='mr5'>可连接：<font color="green">是</font></span>
          <span class='mr5'><font>魔力值(0魔力/小时)</font>
            [<a href="mybonus.php" class="fcb">使用&amp;说明</a>]: 987654.3[<a href="attendance.php?type=sign&amp;uid=12345" class="fcb">签到领魔力</a>]</span>
        </div>
      </td>
    </tr>
  </table>
  <!-- 资料区：用户名、用户等级字段（真实结构，等级文字后还跟着校验链接等杂项） -->
  <table class="main" width="100%">
    <tr>
      <td class="rowhead nowrap">用户名</td>
      <td class="rowfollow">
        <a  href="userdetails.php?id=12345" class='PowerUser_Name'><b>fixture_user</b></a>
        <b title='发布者：0级'>🎈</b>
      </td>
    </tr>
    <tr>
      <td class="rowhead nowrap">用户等级</td>
      <td class="rowfollow">
        <b class="fcr" alt="(小学)Power User" title="(小学)Power User">(小学)Power User</b>
        <a class='fcb' href='action.php?type=vertify_class'>🩺校验等级</a>
      </td>
    </tr>
  </table>
  <!-- 关键回归场景：页面还会出现其他用户，旧 a[class*='_Name'] 会误匹配 -->
  <section id="comments">
    <a href="userdetails.php?id=67890" class="CrazyUser_Name">
      <b>another_user</b>
    </a>
    发布了一条评论。
  </section>
  <!-- 同一账号在页面中的其他重复链接 -->
  <footer>
    <a href="userdetails.php?id=12345" class="PowerUser_Name">
      <b>fixture_user</b>
    </a>
  </footer>
</body>
</html>
"""


def _build_pttime_site() -> tuple[NexusPHPSite, MagicMock]:
    load_all_sites()
    config = get_site_config("pttime")
    assert isinstance(config.selectors, NexusPHPSelectors)
    client = MagicMock()
    response = MagicMock()
    response.text = PTTIME_PROFILE_HTML
    client.get = AsyncMock(return_value=response)
    site = NexusPHPSite(
        selectors=config.selectors,
        category_map=config.category_map,
        site_id=config.site_id,
        base_url=config.base_url,
        client=client,
        auth_manager=MagicMock(),
    )
    return site, client


async def test_pttime_profile_fixture_parses_all_fields() -> None:
    site, client = _build_pttime_site()

    profile = await site.get_user_profile()

    assert profile.username == "fixture_user"
    assert profile.user_id == "12345"
    assert profile.user_class == "(小学)Power User"
    assert profile.ratio == 3.14
    # 字节数由 _parse_size_bytes 取整返回（"12.34TB" → int(12.34 * 1024**4)）。
    assert profile.uploaded_bytes == int(12.34 * 1024**4)
    assert profile.downloaded_bytes == int(5.67 * 1024**4)
    assert profile.bonus == 987654.3
    assert profile.seeding_count == 12
    assert profile.leeching_count == 3

    client.get.assert_awaited_once_with(
        "https://www.pttime.org/userdetails.php", params={}
    )


async def test_pttime_profile_never_leaks_other_users() -> None:
    """旧 a[class*='_Name'] 全页宽匹配会把评论区其他用户拼进用户名，必须杜绝。"""
    site, _ = _build_pttime_site()

    profile = await site.get_user_profile()

    assert profile.username == "fixture_user"
    assert "another_user" not in profile.username
    assert "mcc1127" not in profile.username
    assert profile.user_id == "12345"
