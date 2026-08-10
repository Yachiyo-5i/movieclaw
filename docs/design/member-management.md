# 成员管理：把系统分享给家人朋友——设计

> 状态：设计草案（调研 2026-08-10，未实施）。
> 关联文档：[jellyfin-compat.md](jellyfin-compat.md)（播放侧多用户的协议基础）、
> [library.md](library.md)（库模型）、[subscription.md](subscription.md)（订阅模型）、
> [cli.md](cli.md)（§10 开放问题①的令牌 scope 讨论，本文一并收口）。

## 0. 定位与第一性

**问题**：movieclaw 当前是严格的单管理员系统——登录即全权限。但媒体库产品的
真实使用形态是家庭共享：部署者（管理员）把系统分给家人朋友用，这些人需要
浏览、播放、点播（订阅），却绝不应该碰到站点凭据、下载器、AI 助手（内含
bash）、文件系统浏览这些等价于服务器控制权的功能。

**目标形态**：一个管理员 + 若干成员。成员各看各的进度、各自的收藏，能自助
"想看什么就订什么"；管理员保留全部系统管理能力，并决定每个成员能见哪些库、
能不能发起订阅。

**产品原则**（沿用全库既有铁律的风格）：

1. **默认拒绝**：成员能做什么靠白名单声明，新增接口不声明就是管理员专属——
   与现有"匿名必 401"守护测试同构，升级为"成员越权必 403"守护测试；
2. **超管不动**：超管账号继续留在 `auth.admin` 配置域（作者已有明确决策：
   "即使将来出现新的用户体系，也与这个超管账号无关"，见
   `settings/schemas.py:61-74`），一次性建号锁原样保留，成员体系另起炉灶；
3. **一份下载，多人共享**：家庭场景下资源是公共的（同一块盘、同一批种子），
   成员维度只隔离"个人体验数据"（进度/收藏/偏好/订阅归属），不隔离资源本身。

## 1. 现状盘点（权限视角）

调研结论（2026-08-10，基于当前 main）：

**全库没有 user 实体。** 管理员是 `app_setting` 里 `auth.admin` 域的一条 JSON
（`settings/schemas.py:77-90`）；`require_login`（`api/deps.py:41-60`）返回的是
字符串身份标识（用户名 / `agent:<sid>` / `token:<name>`），仅用于日志归因，
不参与任何授权判断。授权模型是二值的：登录 = 全权限。

**已有的有利条件**：

| 条件 | 位置 | 对本设计的意义 |
|---|---|---|
| 路由三区挂载（公开/插件/受保护） | `api/router.py:48-84` | 加权限层只需把受保护区再分两组，改动集中在一个文件 |
| "匿名必 401"守护测试（真发请求遍历 OpenAPI） | `tests/api/test_auth.py:231` | 模式可原样复制为 403 守护测试 |
| 启动自动迁移 | `movieclaw_db/migrations.py:34` | 用户升级镜像即得新表，无手工步骤 |
| Jellyfin 协议天生多用户（UserDto/Policy/EnabledFolders） | `movieclaw_jellyfin/identity.py:53` | 播放侧多用户几乎是"把硬编码换成真数据" |
| `playback_state` 预留注释"将来加列即可" | `models/playback_state.py:21` | 作者已为 per-user 化留了口子 |
| 领域层解耦（media_item 全局锚、playback 协议无关） | `movieclaw_playback/` | 加 member 维度不会外溢到元数据层 |

**必须正面处理的约束**：

| 约束 | 位置 | 处理 |
|---|---|---|
| `require_login` 返回裸字符串，全站 17 处引用 | `api/deps.py:41` | 升级为 Principal 对象（§3.2），最大改造面 |
| `subscription` 上 `UNIQUE(media_item_id)` | `models/subscription.py:44` | 两个成员订同一部剧会撞约束，用 follower 表化解（§3.5） |
| `library` 无任何可见性字段 | `models/library.py` | 可见性挂成员侧白名单（§3.6） |
| 登录限速是全局单个计数器 | `services/auth.py:107` | 按用户名分桶，否则一个成员输错密码锁住全家（§3.3） |
| PAT / Agent 令牌与管理员完全同权 | `services/auth.py:278-337` | PAT 创建收为管理员专属，cli.md 开放问题①一并收口（§3.8） |
| IM 通道白名单绑定的是"平台用户 id"非成员 | `models/channel_account.py:33` | P2 再接成员体系，P1 维持管理员专属（§3.8） |
| Jellyfin 登录失败会累加 Web 端全局限速 | `movieclaw_jellyfin/routes/users.py:49` | 分桶后自然解耦（同一用户名同一桶，语义反而更对） |
| 改密码轮换全局签名密钥、全端下线 | `services/auth.py:200-211` | 成员会话改用 token_version 校验，改密只踢自己（§3.3） |

**高危面清单**（成员在任何阶段都绝不可及）：

- `/agent`——AI 助手内含 bash 工具，等价 shell；
- `/fs`——任意目录浏览；
- `/sites`——PT 站点凭据，泄露 = 账号被封；
- `/downloaders`（含 `/submit` 直投）、`/import-watch`、`/rule-sets`；
- `/llm`、`/network`、`/webhook`、`/app`（含重启）、`/system/logs`、`/channels/*`；
- `/auth/tokens`（PAT 创建，否则成员自发 PAT 即完成提权）；
- `/subscriptions/dispatch-preview`、`/pipeline-health`、`/{id}/grab`、
  `/{id}/downloads`（暴露落盘路径/种子/下载器细节）。

## 2. 产品设计

### 2.1 角色模型选型

- **方案 A：完整 RBAC（角色表 + 权限表 + 关联表）**——表达力过剩。家庭场景
  不存在"给三姨妈单独定义一个角色"的需求，多两张表多一套要学的概念，否决；
- **方案 B：两级固定角色（超管 / 成员）+ 成员级能力开关**——Jellyfin/Emby
  的成熟形态（Policy flags），家庭用户已有心智模型。**采用**；
- **方案 C：只做 Jellyfin 侧多用户，不做 Web 成员**——播放隔离了，但"家人
  自助订阅"这个核心诉求落空，且 Web 端仍是共享管理员密码，否决。

### 2.2 成员能做什么

**成员基线能力**（登录即有，不可关）：

- 浏览可见库、条目详情、演职员页、图片；
- 播放（Web 播放器与 Jellyfin 客户端），各自的进度/已看/收藏；
- 发现页（TMDB 热门 / 豆瓣 Top250）；
- 个人设置：昵称、头像、密码、外观。

**成员能力开关**（每成员独立，管理员配置）：

| 开关 | 默认 | 说明 |
|---|---|---|
| `allow_subscribe` | 开 | 发起订阅 / 关注已有订阅；可查看并暂停/删除自己发起的订阅 |
| `allow_search` | 关 | 站点聚合搜索。消耗站点配额、暴露站点存在，默认不给 |
| `library_access` | 全部 | 可见库白名单，`null` = 全部库（含以后新建的） |

**管理员专属**（除上述外的一切）：库 CRUD/扫描/刮削/整理/删除、订阅链路
运维（grab/体检/预览）、全部系统设置、成员管理本身。

刻意不做（v1 否决，避免过度设计）：订阅审批流（成员订了管理员批——家庭
场景下微信喊一嗓子比工作流快）、按成员的下载配额/限速、成员分组、
细粒度到"每个按钮"的权限点。真实需求出现再加。

### 2.3 交互形态

- **登录页不变**：同一个登录框，用户名区分超管与成员；
- **成员登录后的界面 = 现界面做减法**：侧边栏保留"新任务 / 媒体库 / 我的
  订阅"（现主导航天然就是使用面），设置页只剩"个人信息 / 外观"两个分区，
  条目详情页隐藏删除/重识别/整理等管理操作；
- **成员管理入口**：设置页新增"成员"分区（仅超管可见）：成员列表、新建
  （用户名+初始密码）、启用/停用、重置密码、编辑能力开关与可见库；
- **Jellyfin/Emby 客户端**：`/Users/Public` 返回超管 + 启用中的成员，电视端
  登录页自动出现多个头像，各人登录各人的账号，进度互不干扰。

## 3. 技术设计

### 3.1 数据模型：新建 `member` 表

按 `search_history.py:12-26` 写明的判据（持续增长、逐条增删、需外键引用的
列表数据建独立表，而非塞 `app_setting`），成员建表：

```python
class Member(TimestampMixin, table=True):
    """成员账号。超管不在本表（见 auth.admin 配置域的决策注释）。"""
    __tablename__ = "member"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)   # 登录名，与超管用户名互斥
    password_hash: str                                # argon2，复用 pwdlib
    nickname: str = ""
    avatar_path: str | None = None
    status: str = "active"                            # active / disabled
    token_version: int = 0                            # 改密/停用时 +1，旧会话即失效
    allow_subscribe: bool = True
    allow_search: bool = False
    library_access: list[int] | None = None           # JSON；None = 全部库可见
```

要点：

- **超管不迁入**。`auth.admin` 配置域、一次性建号锁（`services/auth.py:128`）
  原样保留。代价是鉴权层要处理两种身份来源，收益是零数据迁移、不触碰全库
  最硬的安全约束；
- **用户名互斥**：建成员时校验与超管用户名不同（大小写不敏感），登录时先查
  超管再查 `member` 表，语义无歧义；
- `library_access` 用 JSON 列而非关联表——与 `library.root_paths`、
  `match_rules` 的既有先例一致，家庭场景成员数与库数都是个位数，关联表是
  过度设计。库被删除时白名单里的悬空 id 直接忽略（等价于不可见），无需
  级联清理；
- 新模型必须在 `movieclaw_db/models/__init__.py` 注册，否则 Alembic
  autogenerate 看不到（该文件 docstring 的硬规则）。

### 3.2 鉴权层：Principal 对象 + 双依赖 + 双守护测试

**`require_login` 的返回值从 `str` 升级为 `Principal`**——这是全设计最大的
改造面（全站 17 处引用），但绝大多数引用只拿它做日志归因，机械替换即可：

```python
@dataclass(frozen=True)
class Principal:
    """请求主体：鉴权层产出、授权层消费的统一身份对象。"""
    kind: str                 # "admin" / "member" / "pat" / "agent"
    name: str                 # 展示名（用户名 / token:<name> / agent:<sid>）
    member_id: int | None     # 仅 kind == "member" 时非空
    is_admin: bool            # admin / pat / agent 均为 True（见 §3.8）

    def __str__(self) -> str: ...  # 兼容现有日志格式，降低替换成本
```

**依赖分层**：

- `require_login` → 任何合法主体（成员 + 超管），行为不变；
- `require_admin` → 在 `require_login` 之上断言 `is_admin`，否则 403
  `FORBIDDEN`（中文错误信息："该操作需要管理员权限"）；
- 能力开关在服务层按 `Principal` 判定（如 `allow_subscribe`），因为它们
  出现在"混合路由器"内部，粒度到不了 router 级。

**路由挂载**：`api/router.py` 的 `_PROTECTED_ROUTERS` 拆为两组：

```
_MEMBER_ROUTERS   require_login   discover / people / images / ui / appearance /
                                  system-notices / libraries(读) / subscriptions(部分) /
                                  search(受 allow_search) / playback
_ADMIN_ROUTERS    require_admin   sites / downloaders / llm / network / app /
                                  app-update / logs / import-watch / webhook /
                                  rule-sets / channels / fs / agent / spec / extension(管理侧)
```

混合路由器（`libraries`、`subscriptions`、`auth`、`search`）在 router 级挂
`require_login`，管理动作逐路由加 `Depends(require_admin)`。`libraries` 当前
是 2000+ 行 47 条路由的单文件，正好借本次改造按"浏览 / 管理"拆为两个
router 文件分别挂载（属于本改动的直接产物，不是顺手重构）。

**守护测试升级为双层**：

1. 现有"匿名必 401"测试不动；
2. 新增"成员越权必 403"测试：用成员会话真发请求遍历 OpenAPI 全部路由，
   路由必须要么在成员白名单清单里（测试文件内显式维护，评审可见），要么
   返回 403。新增路由不声明归属就 CI 红——把"默认拒绝"延伸到权限层。

### 3.3 会话与登录

**成员会话令牌**：复用同一把 `SessionSecretSetting.secret` 与 itsdangerous
签名机制，负载扩展为 `{"u": 用户名, "k": "member", "mid": id, "ver": token_version,
"exp": ...}`。旧格式（无 `"k"` 字段）继续按超管解释——已登录的管理员升级后
**不掉线**，向前兼容。

**失效语义**（修复"一人改密踢全家"）：

- 成员改密码 / 被停用 / 被重置密码 → `member.token_version += 1`，校验时
  比对负载 `ver`，不匹配即 401。只踢这一个人；
- 超管改密码 → 维持现状（轮换全局密钥、全端下线）。这是可接受的语义：
  超管密钥可能泄露时，全体下线是正确行为；
- 代价：成员会话校验从纯无状态变为"验签 + 查一行 member"。member 行走
  内存缓存（进程内 dict，写操作时失效），单机 SQLite 场景开销可忽略。

**登录流程**：`POST /auth/login` 不变，服务层先匹配超管（`secrets.compare_digest`
+ argon2，现有逻辑），未命中再查 `member` 表。防探测语义保持：无论走到哪一步
都执行一次密码哈希校验，失败信息不区分"用户名不存在/密码错误"。

**限速分桶**：`LoginThrottle` 从模块级单例改为按用户名分桶（dict + 上限保护，
如 LRU 128 桶防内存注水），阈值/退避参数不变。Jellyfin 侧
`AuthenticateByName` 复用同一入口，电视上输错密码只锁该用户名，不再连坐
Web 控制台。

### 3.4 个人数据 per-member 化：`playback_state`

`playback_state` 加 `member_id` 列。关键细节：**不能用 nullable 列进唯一
约束**——SQLite（及标准 SQL）的 UNIQUE 视 NULL 互不相等，`(NULL, item, s, e)`
可以插无限行。因此：

```
member_id INTEGER NOT NULL DEFAULT 0     -- 0 = 超管（哨兵值，不是外键）
UNIQUE(member_id, media_item_id, season_number, episode_number)
```

存量行迁移后 `member_id = 0`，即"历史进度归超管"，语义正确（此前只有超管
在用）。SQLite 改唯一约束走 `render_as_batch` 建新表拷贝，Alembic 配置已就绪
（`alembic/env.py:53`）。这是单向前进迁移，回退跨版本靠更新前自动备份
（发布规范第 3 条），无兼容问题。

`movieclaw_playback` 领域层（`state.py / progress.py`）的读写入口统一加
`member_id` 参数，Web 播放器与 Jellyfin 共用一套，改一处两侧同时生效。

搜索历史（`search_history`）、搜索偏好、UI 偏好当前是全局单例，P2 再
per-member 化（§4），P1 阶段成员用系统默认值即可，不阻塞主线。

### 3.5 订阅归属：发起人 + 关注表

保留 `UNIQUE(media_item_id)`（"同一作品全局一份订阅"是资源共享场景的正确
模型——两个人订同一部剧不应下两份），在其上补两块：

```
subscription.created_by_member_id  INTEGER NULL      -- NULL = 超管发起
subscription_follower(subscription_id, member_id, UNIQUE(两列))
```

行为：

- 成员 A 订阅《某剧》→ 正常建订阅，`created_by_member_id = A`；
- 成员 B 再订同一部 → 幂等命中已有订阅，为 B 写一行 follower，B 的"我的
  订阅"里也看得到它（现有服务层本就对重复订阅幂等返回，只是多写一行）；
- B "取消订阅" → 只删自己的 follower 行；A（发起人）取消且无其他 follower
  → 真删订阅（连带未完成工单，现逻辑）；有 follower → 转移发起人给最早的
  follower，订阅继续活着。**成员的取消永远不影响别人正在追的内容**；
- 修改订阅（改季、暂停）：发起人与超管可操作；follower 只能取关；
- 缺口计算（期望 − 库存）与工单生命周期完全不变——期望集合仍是订阅自身的
  `selected_seasons`，follower 不扩集。B 想追 A 没选的季，提示"联系管理员
  或发起人调整"（v1 不做并集调和，避免"B 取关后要不要缩回"这类状态难题）；
- 季集选择、规则组、目标库沿用现有路由与定格机制；规则组是管理员配置，
  成员订阅一律用默认规则组与自动路由结论（成员没有"选库/选规则"的 UI）。

成员可见的订阅列表 = 自己发起的 + 自己关注的；超管看全部并展示发起人。

### 3.6 库可见性

过滤实现在服务层单点收口（`library` 列表查询处），消费面：

- `GET /libraries` 及全部 `/libraries/{id}/...` 读接口：白名单外的库 404
  （不是 403——不泄露"存在但你不能看"）;
- 全局搜索 `GET /libraries/search`：结果按可见库过滤；
- Jellyfin `/UserViews`、`/Items` 层级导航：按成员可见库投影（§3.7）；
- 发现页/详情页的"已入库"徽标：按可见库计算，避免"显示已入库但点进去 404"。

**可见性不影响下载链路**：成员发起的订阅照常走库路由（可能落到一个他看不到
的库）——资源是公共的，可见性只是浏览隔离。订阅详情对成员只展示库名不展示
路径。若这个语义在实际使用中反直觉（"我订的东西我看不到"），再考虑"订阅
路由限制在可见库内"，属于可逆的服务层小改。

### 3.7 Jellyfin 兼容层多用户

协议天生多用户，改造是"把硬编码换真数据"：

| 改造点 | 现状 | 改为 |
|---|---|---|
| `jellyfin_device` 表 | 无用户维度 | 加 `member_id INTEGER NOT NULL DEFAULT 0`（0=超管） |
| `AuthenticateByName` | 只认超管 | 复用 §3.3 统一登录入口，按命中身份落 device |
| `user_guid()` | 固定编码单 GUID | 超管保持原 GUID（已配对的客户端不掉线），成员按 `member:{id}` 派生 |
| `/Users/Public` | 单元素数组 | 超管 + `status=active` 的成员 |
| `user_policy()` | 全硬编码 `IsAdministrator: True` | 按身份投影：成员 `IsAdministrator: False`、`EnableAllFolders: False` + `EnabledFolders` 填可见库 GUID、`EnableContentDeletion: False` |
| `/Users/{user_id}/...` 的 user_id | 一律忽略 | 校验与 token 身份一致，不一致 403 |
| 播放进度上报 | 全局 | 走 §3.4 的 member 维度 |

设备 token 语义不变（长期有效、`device_id` 覆盖换发），停用成员时删除其
全部 `jellyfin_device` 行（协议侧无 token_version 机制，直接删行最简单）。

### 3.8 令牌与旁路入口收口

- **PAT**：`/auth/tokens*` 三条路由挂 `require_admin`。成员无法创建 PAT，
  存量 PAT 继续等价管理员（它们本就是超管创建的）。这同时收掉 cli.md
  开放问题①——不做 scope 分级，做"创建权限收口"，更简单且足够；
- **Agent 令牌**：Agent 运行本身已是管理员专属（`/agent` 在 admin 组），
  其工作区令牌维持 `is_admin=True`（Agent 需要回调各管理接口），现有
  "禁止递归"硬闸保留；
- **插件同步令牌**：独立密钥体系不动，令牌管理路由归 admin 组；
- **IM 通道**：P1 维持现状（`bound_user_id` 单人白名单 = 超管本人）。P2 把
  绑定升级为"平台用户 ↔ member"映射表，成员经 IM 只能走受限指令集
  （订阅/查询），**绝不接入 Agent 会话**——IM 现在走 Agent 而 Agent 有
  bash，这是成员接入 IM 前必须先堵上的提权通道。

### 3.9 前端

- `SessionView` 扩展：`{ username, nickname, avatar_url, role: "admin"|"member",
  capabilities: { allow_subscribe, allow_search } }`，由 `GET /auth/me` 返回；
- `SessionProvider` 之上不需要新门禁组件：`AppShell` 按 `role` 裁剪侧边栏与
  user-menu，设置页按 `role` 过滤分区清单（成员只剩 profile / appearance），
  条目详情等页面按 `role` 隐藏管理操作按钮。注释里已有的原则继续成立：
  **前端裁剪只是体验，安全边界在后端 403**；
- 设置页新增"成员"分区（admin only）：列表 / 新建 / 启用停用 / 重置密码 /
  能力开关 / 可见库多选（复用现有库列表接口）；
- 成员的 `/settings/profile` 复用现有头像/昵称/改密组件，后端对应接口
  （`/auth/profile`、`/auth/avatar`、`/auth/password`）按 Principal 分流到
  member 表。

### 3.10 迁移与发布

- 新增：`member`、`subscription_follower` 两张表；加列：
  `playback_state.member_id`、`subscription.created_by_member_id`、
  `jellyfin_device.member_id`。全部是"新表 + 带默认值的加列"，符合
  "迁移只能向前兼容"的发布铁律；唯一约束重建走 batch 模式；
- 不动运行时依赖（纯 Python 业务代码 + 迁移），**无需 bump
  `docker/runtime-version`**；
- 升级路径：老版本升上来自动建表，超管无感；回退跨版本靠更新前自动备份
  （既有机制）。

## 4. 实施分期

每期独立可合并、可验证（守护测试即验收标准）：

**P0——身份与权限骨架**（其余各期的地基）

1. `member` 表 + 迁移 + Repository → 验证：模型注册、迁移可升；
2. Principal 化 `require_login`、新增 `require_admin`、路由分组拆分
   → 验证：现有 401 守护测试全绿（行为不回归）；
3. 成员登录（含限速分桶、token_version 失效）→ 验证：成员登录/停用/改密
   的会话生命周期测试；
4. "成员越权必 403"守护测试 + 成员白名单清单 → 验证：CI 遍历 OpenAPI 全绿；
5. 前端：SessionView 扩展、导航/设置页按角色裁剪、成员管理分区
   → 验证：`pnpm web:lint` / `web:typecheck`，成员登录走查。

**P1——个人体验数据隔离**

6. `playback_state` 加 member 维度（含唯一约束重建）→ 验证：两个账号进度
   互不覆盖的集成测试；
7. 库可见性过滤（服务层单点 + 各消费面）→ 验证：白名单外的库对成员 404；
8. Jellyfin 多用户投影（§3.7 全部改造点）→ 验证：`/Users/Public` 多头像、
   成员 policy 的 `EnabledFolders` 正确、跨成员进度隔离；
9. 订阅归属 + follower（§3.5）→ 验证：双成员订同一作品幂等成 follow、
   取消互不影响的用例。

**P2——外围收口（按需求热度排期）**

10. 搜索历史 / 搜索偏好 / UI 偏好 per-member 化；
11. IM 通道绑定成员化（受限指令集，不接 Agent）;
12. 视需求：订阅审批流、成员配额、`allow_search` 细化（限站点/限频）。

## 5. 开放问题（需要用户拍板）

1. **成员发起订阅是否默认放开**？本设计默认 `allow_subscribe = 开`（"家人
   自助点播"是核心场景），若担心失控可改默认关、逐人打开；
2. **成员站点搜索**默认关是否符合预期？开了就意味着成员行为消耗 PT 站点
   配额、且能看到你接入了哪些站点；
3. **库可见性不影响订阅投递**（§3.6）的语义是否接受——成员订的内容可能落
   在他看不到的库里；替代方案是"成员订阅只路由到可见库"；
4. **Jellyfin 侧超管 GUID 保持不变**意味着已配对的电视端升级后仍以超管身份
   登录——家里电视原本是公用的，升级后建议为电视重新用成员账号登录，是否
   需要在更新说明里显式提醒。
