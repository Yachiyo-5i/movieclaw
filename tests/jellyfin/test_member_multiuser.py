"""Jellyfin 兼容层多用户行为（docs/design/member-management.md §3.7）。

覆盖三块：
1. 登录页与身份投影：/Users/Public 多头像、成员 Policy 非管理员 +
   EnabledFolders 按可见库下发；
2. 库可见性服务端强制：白名单外的库对成员在**枚举与直达两条路径**上
   都不可及——UserViews 过滤、条目详情/播放协商/取流/下载一律 404
   （GUID 是可枚举的结构化编码，只挡浏览不挡直达等于没挡）；
3. 观看状态按登录身份隔离：成员标记已看不影响超管的 UserData。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from movieclaw_jellyfin.ids import episode_guid, item_guid, library_guid

from .helpers import ADMIN, jf_login

MEMBER = {"username": "family", "password": "family-pass-1"}
MEMBER_AUTH_HEADER = (
    'MediaBrowser Client="Infuse", Device="Kid TV", '
    'DeviceId="member-device-1", Version="8.2"'
)


def _create_member(client: TestClient, *, tv_lib_id: int) -> None:
    """经 Web API 建成员并把可见库限定为剧集库（client 已带超管 Cookie）。"""
    created = client.post("/api/v1/members", json=MEMBER)
    assert created.status_code == 200, created.text
    member_id = created.json()["data"]["id"]
    updated = client.put(
        f"/api/v1/members/{member_id}",
        json={"all_libraries": False, "library_ids": [tv_lib_id]},
    )
    assert updated.status_code == 200, updated.text


def _member_login(client: TestClient) -> str:
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": MEMBER["username"], "Pw": MEMBER["password"]},
        headers={"Authorization": MEMBER_AUTH_HEADER},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["AccessToken"]


def test_member_identity_projection(client: TestClient, seeded: dict) -> None:
    """多头像登录页；成员 Policy 投影非管理员 + EnabledFolders。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])

    public = client.get("/Users/Public").json()
    assert [u["Name"] for u in public] == [ADMIN["username"], MEMBER["username"]]
    assert public[0]["Policy"]["IsAdministrator"] is True
    assert public[1]["Policy"]["IsAdministrator"] is False
    assert public[1]["Policy"]["EnableAllFolders"] is False
    assert public[1]["Policy"]["EnabledFolders"] == [library_guid(seeded["tv_lib"])]

    # 登录响应与 /Users/Me 的身份一致，且不是超管的固定 GUID
    token = _member_login(client)
    me = client.get("/Users/Me", headers={"X-Emby-Token": token}).json()
    assert me["Name"] == MEMBER["username"]
    assert me["Id"] == public[1]["Id"]
    assert me["Id"] != public[0]["Id"]


def test_member_library_visibility_enforced_on_all_paths(
    client: TestClient, seeded: dict
) -> None:
    """白名单外的电影库：枚举不出现，直达（详情/协商/取流/下载）全 404。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])
    token = _member_login(client)
    headers = {"X-Emby-Token": token}
    movie_guid = item_guid(seeded["movie"])

    views = client.get("/UserViews", headers=headers).json()
    assert [v["Name"] for v in views["Items"]] == ["剧集"]

    assert client.get(f"/Items/{movie_guid}", headers=headers).status_code == 404
    info = client.get(f"/Items/{movie_guid}/PlaybackInfo", headers=headers).json()
    assert info["MediaSources"] == []
    stream = client.get(
        f"/Videos/{movie_guid}/stream", params={"static": "true"}, headers=headers
    )
    assert stream.status_code == 404
    assert (
        client.get(f"/Items/{movie_guid}/Download", headers=headers).status_code == 404
    )

    # 可见库内的剧集一切正常（对照组，防止把门修成了全关）
    show_guid = item_guid(seeded["show"])
    assert client.get(f"/Items/{show_guid}", headers=headers).status_code == 200
    admin_token = jf_login(client)
    assert (
        client.get(f"/Items/{movie_guid}", headers={"X-Emby-Token": admin_token}).status_code
        == 200
    )


def test_member_playstate_isolated_from_admin(client: TestClient, seeded: dict) -> None:
    """成员标记已看只写自己的行：超管同一集的 UserData 不受影响。"""
    _create_member(client, tv_lib_id=seeded["tv_lib"])
    member_token = _member_login(client)
    admin_token = jf_login(client)
    ep = episode_guid(seeded["show"], 1, 1)

    marked = client.post(
        f"/UserPlayedItems/{ep}", headers={"X-Emby-Token": member_token}
    )
    assert marked.status_code == 200
    assert marked.json()["Played"] is True

    admin_item = client.get(f"/Items/{ep}", headers={"X-Emby-Token": admin_token}).json()
    assert admin_item["UserData"]["Played"] is False
    member_item = client.get(f"/Items/{ep}", headers={"X-Emby-Token": member_token}).json()
    assert member_item["UserData"]["Played"] is True
