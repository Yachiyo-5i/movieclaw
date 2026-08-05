# 播放事件 Webhook 设计

> 状态：设计定稿 **v1.0**（2026-08-05），待实施。
>
> 源起：issue #86（@kriskwok 提出）——Infuse 等播放器已通过 Jellyfin 兼容接口把播放
> 进度/已看状态写进 MovieClaw，但 Yamtrack、Home Assistant、自建统计等外部服务无法
> 及时获知这些变化，只能读库或轮询，与内部表结构耦合。需要一个可选的、通用的播放
> 事件 Webhook。
>
> 依据：main 分支 `b0e19b7` 的代码调研；jellyfin-plugin-webhook 官方文档（模板变量
> 与 Handlebars 助手清单）；Stripe / GitHub webhook 的管理与签名惯例。
>
> 关联文档：`docs/design/jellyfin-compat.md`（播放状态写入链路、§8.5 领域层/协议层
> 分离原则）。
>
> **本文档即实现契约**：对外报文格式、字段稳定性承诺、事件映射规则以本文为唯一裁判。

## 0. 定位与硬决策

定位一句话：**播放状态发生变化后，MovieClaw 主动向用户配置的一个或多个 HTTP
endpoint 推送 JSON 事件；每个 endpoint 可选「自有协议」或「Jellyfin 兼容」两种外发
格式，前者面向新接入方，后者让已适配 Jellyfin Webhook 插件的下游免适配接入。**

以下决策实现时不可突破：

1. **触发点在协议层 handler、`session.commit()` 之后**。不放领域层，理由有三：
   ① 领域函数在 commit 前返回，领域层内发事件会推送尚未落库的状态；
   ② `stopped` 与 `completed` 在领域层无法区分（Jellyfin 协议里 Progress 与 Stopped
   共用 `record_playback_progress`，只有协议层知道本次上报是不是 Stopped）；
   ③ 客户端信息（Infuse / Apple TV）只在协议层的 `RequestIdentity` 里可达。
   领域层（`movieclaw_playback`）保持协议无关；未来网页播放器的 handler 调用同一个
   emit 入口即可接入。
2. **内部规范事件与外发格式分层**。`PlaybackEvent`（内部 dataclass）按两种格式的
   字段并集装配一次，formatter 只做映射与渲染、不回查数据库。将来要支持 Emby 格式
   也只是再加一个 formatter。
3. **推送绝不影响业务主链路**。同步签名 + `asyncio.create_task` fire-and-forget、
   任务强引用集合防 GC、异常全吞只记日志——完全对齐 `services/channel_push.py`
   的既有惯例。
4. **出站必须走 `movieclaw_net.egress_transport`**，新增服务标签 `webhook` 并在
   `BUILTIN_EGRESS_SERVICES` 登记；endpoint 可逐个选择 `LAN`（内网直连，默认）或
   `WAN`（走代理）出口。
5. **内部单位毫秒、时间 RFC3339 带时区、字段 snake_case**。ticks 只允许出现在
   Jellyfin formatter 的边界换算里（`position_ms × 10000`），与兼容层"入站把 ticks
   归一为 ms"方向对称。
6. **自有协议字段只增不改不删**。信封带 `spec_version`，演进只做加法，消费端必须
   忽略未知字段——与"数据库迁移只向前兼容"同一价值观。
7. **P1 零数据库迁移、零新增运行时依赖**。配置走 `app_setting`
   （namespace `webhook.playback`）；HMAC 用标准库 `hmac`/`hashlib`；ULID 自实现
   （约 20 行，`os.urandom` + 时间戳，避免引入 `python-ulid` 触发
   `docker/runtime-version` bump）。**本功能 P1 无需 bump runtime-version。**

有意偏离清单：

- ① 偏离 issue 原始建议的「环境变量配置」——改走设置页 + `app_setting`，理由：
  项目三层配置边界（`settings/__init__.py` 备忘）规定运行时业务配置不走 env，且
  Stripe 式管理页需要运行时增删 endpoint。
- ② 偏离 Jellyfin Webhook 插件的「无签名」惯例——自有格式强制 HMAC 签名；
  Jellyfin 兼容格式为保持下游行为一致不签名，但支持自定义 header 供下游鉴权。

## 1. 事件模型

### 1.1 事件类型

| 事件 | 触发时机 | 分期 |
| --- | --- | --- |
| `playback.started` | 客户端上报开始播放（`record_playback_start`） | P1 |
| `playback.stopped` | 客户端上报停止播放 | P1 |
| `playback.completed` | 本次上报使 `played` 从 False 翻转为 True（阈值判定见 `movieclaw_playback/progress.py`） | P1 |
| `playback.marked_played` | 手动标记已看（含整季/整剧级联，逐单元发） | P1 |
| `playback.marked_unplayed` | 手动取消已看（同上） | P1 |
| `playback.progress` | 播放进度上报，按单元节流（默认间隔 30s），**默认关闭** | P2 |
| `webhook.test` | 设置页「发送测试」，载荷为示例数据 | P1 |

收藏（favorite）事件 P1 不做（issue 未要求；且整剧/整季收藏使用 `(id, season, -1)`
哨兵单元，不是真实播放单元），见 §8 开放问题。

### 1.2 触发点映射（`src/movieclaw_jellyfin/routes/playstate.py`）

legacy 别名路由与主路由共享同一批 handler，因此埋点只有 6 处：

| handler | 触发事件 |
| --- | --- |
| `playing_start`（含 legacy） | `started` |
| `playing_progress`（含 legacy） | `completed`（仅当 played 翻转） |
| `playing_stopped`（含 legacy） | `stopped`；若本次同时翻转 played，追加 `completed` |
| `mark_played` | `marked_played` × N（级联单元，共享 `batch_id`） |
| `mark_unplayed` | `marked_unplayed` × N（同上） |
| `mark_favorite` / `unmark_favorite` | P1 不触发 |

`playing_ping` 心跳本就不落库，不触发任何事件。

配套的领域层小改动（保持协议无关）：`record_playback_progress` 的返回值补充
`newly_played: bool`（played 是否在本次从 False 翻转为 True），供协议层判定
`completed`；`mark_played` 已返回级联单元列表，无需改动。

### 1.3 规范事件 `PlaybackEvent`

定义在 `src/movieclaw_playback/events.py`（领域包，不依赖任何协议包）：

```
PlaybackEvent:
  event_id: str            # ULID，重试不变，消费端幂等去重键
  event: str               # 上表事件名
  occurred_at: datetime    # 带时区
  batch_id: str | None     # marked_* 级联时同批共享，其余为 None
  media:                   # 装配自 media_item（一次 JOIN）
    item_id: int           # media_item.id，同时用于生成 Jellyfin ItemId
    type: "movie"|"episode"
    tmdb_id: int           # 必填唯一锚（media_item.tmdb_id 非空唯一）
    imdb_id: str | None
    title: str / original_title / year
    season_number / episode_number: int | None   # 电影为 None（内部 (0,0) 哨兵不外泄）
  playback:
    position_ms / duration_ms: int | None
    played: bool
    play_count: int
  client:                  # 可空；来自协议层 RequestIdentity，未来网页端可能没有
    name / device_name / device_id / version
```

装配函数 `assemble_playback_events(session, units, ...)` 在同文件实现，JOIN
`media_item` 取标识与标题；**不复用** `movieclaw_jellyfin/catalog.py` 的
`load_bundles`（避免领域层反向依赖协议包，且它装载的内容远超所需）。

## 2. 自有协议（`format: movieclaw`）

### 2.1 报文

`POST <url>`，`Content-Type: application/json; charset=utf-8`，UTF-8 snake_case：

```json
{
  "spec_version": "1",
  "event_id": "01JG8YQ4TZKX5H3M9W2E7R6VBN",
  "event": "playback.completed",
  "occurred_at": "2026-08-04T20:30:00+08:00",
  "server": { "id": "a1b2c3", "name": "MovieClaw", "version": "1.4.0" },
  "media": {
    "type": "episode",
    "tmdb_id": 1399,
    "imdb_id": null,
    "title": "权力的游戏",
    "year": 2011,
    "season_number": 2,
    "episode_number": 4
  },
  "playback": {
    "position_ms": 3120000,
    "duration_ms": 3300000,
    "played": true,
    "play_count": 1
  },
  "client": { "name": "Infuse", "device_name": "Apple TV" },
  "batch_id": null
}
```

### 2.2 字段稳定性承诺

- **稳定字段**（永远出现，取不到值时为 `null`）：`spec_version`、`event_id`、
  `event`、`occurred_at`、`server.id`、`media.type`、`media.tmdb_id`、
  `media.season_number`、`media.episode_number`、`playback.position_ms`、
  `playback.played`——覆盖 issue 要求的全部稳定字段。
- **便利字段**（尽力提供，内容可能随元数据刷新变化）：`media.title`、
  `media.year`、`media.imdb_id`、`playback.duration_ms`、`playback.play_count`。
- **可空对象**：`client` 整体可为 `null`（非播放器入口）；`batch_id` 仅级联时非空。
- 演进只加字段，消费端必须忽略未知字段。

### 2.3 HTTP 头与签名

```
X-MovieClaw-Event: playback.completed
X-MovieClaw-Delivery: <event_id>          ← 重试不变，幂等去重键
X-MovieClaw-Signature: sha256=<hex(HMAC-SHA256(secret, raw_body))>
User-Agent: MovieClaw/<version>
```

secret 由服务端在创建 endpoint 时生成（`mcwh_` 前缀 + 32 字节随机 hex），签名对
**原始请求体字节**计算（GitHub 惯例）。

### 2.4 投递语义

- fire-and-forget，超时 10s，失败重试 2 次（间隔 5s / 30s），共 3 次尝试；
- 语义为 at-least-once（有重试）但**不承诺必达**（无持久队列，进程重启丢弃在途任务）；
- 不保证顺序，消费端按 `occurred_at` + `event_id`（ULID 时间有序）排序；
- 命中 `movieclaw_net` 熔断（`CircuitOpenError`）时快速失败并记入投递记录。

## 3. Jellyfin 兼容格式（`format: jellyfin`，P2）

### 3.1 兼容等式

Jellyfin Webhook 插件没有固定报文——它让用户粘贴 Handlebars 模板，用固定的
**变量字典**渲染后 POST。因此免适配的兼容等式是：

> MovieClaw 提供同名变量字典 + 渲染用户粘贴的同一段模板。

下游文档里写「在 Jellyfin webhook 插件里粘贴这段模板」，用户把同一段粘进
MovieClaw 即可。

### 3.2 NotificationType 映射与去重

| MovieClaw 事件 | Jellyfin NotificationType | 备注 |
| --- | --- | --- |
| `started` | `PlaybackStart` | |
| `stopped` | `PlaybackStop`（`PlayedToCompletion=false`） | |
| `completed` | `PlaybackStop`（`PlayedToCompletion=true`） | Jellyfin 无独立 completed |
| `marked_played` / `marked_unplayed` | `UserDataSaved`（`Played`/`PlayCount` 区分） | |
| `progress` | `PlaybackProgress` | |

**去重规则**：同一次上报同时产生 `stopped` + `completed` 时，自有格式发两条，
Jellyfin 格式**只发一条** `PlaybackStop`（`PlayedToCompletion=true`）——`completed`
吸收 `stopped`。该逻辑放在 formatter 层，佐证内部事件与外发报文必须分层。

### 3.3 变量子集

按插件官方文档实现以下子集（缺失项渲染为空字符串，文档中注明对齐的插件版本）：

- Server：`ServerId` / `ServerName` / `ServerUrl`（复用 `jellyfin.compat` 设置的
  `server_id` / `server_name` / `published_server_url`）、`ServerVersion`、
  `NotificationType`
- Item：`ItemId`（**与 Jellyfin 兼容 API 对外的条目 ID 同源**，下游可回调
  MovieClaw 的 Jellyfin API 查详情拉海报）、`ItemType`、`Name`、`Year`、
  `RunTime` / `RunTimeTicks`、`Provider_tmdb` / `Provider_imdb`、
  `Timestamp` / `UtcTimestamp`
- Episode：`SeriesName`、`SeasonNumber` / `SeasonNumber00`、
  `EpisodeNumber` / `EpisodeNumber00`、`SeriesId`
- Playback：`PlaybackPosition` / `PlaybackPositionTicks`、`PlayedToCompletion`、
  `Played`、`PlayCount`、`Favorite`
- Device：`DeviceName` / `DeviceId` / `ClientName`
- User：`UserId` / `NotificationUsername`（单管理员形态，取兼容层虚拟用户）

P1 不提供媒体流类变量（`Audio_0_*` / `Video_0_*` 等）。

### 3.4 最小 Handlebars 渲染器

Python 无维护良好的 Handlebars 库（pybars3 已停滞），自实现最小子集
（约 150 行纯函数，落在 `movieclaw_api/services/webhook/handlebars.py`）：
`{{Var}}` 插值 + 官方 5 个助手 `if_equals` / `if_exist` / `link_to` /
`url_encode` / `json_encode` + `{{else}}`。这覆盖了下游文档模板的绝对主流写法；
冷门语法按需求迭代。

## 4. 配置与设置页（Stripe 式管理）

### 4.1 设置 Schema

`src/movieclaw_api/settings/webhook.py`，
`@register_setting(namespace="webhook.playback", title="播放事件 Webhook")`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 总开关，默认 False |
| `endpoints` | list | endpoint 列表，见下 |

endpoint 结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 服务端生成（ULID） |
| `name` | str | 显示名，如 "Yamtrack" |
| `url` | str | 目标地址 |
| `format` | `"movieclaw"` \| `"jellyfin"` | 外发格式 |
| `enabled` | bool | 单端点开关 |
| `events` | list[str] | 订阅的事件（多选） |
| `secret` | str | 仅 movieclaw 格式；服务端生成，加密落库 |
| `template` | str \| None | 仅 jellyfin 格式；用户粘贴的 Handlebars 模板 |
| `headers` | dict[str, str] | 自定义请求头（jellyfin 格式下游鉴权用） |
| `egress_scope` | `"lan"` \| `"wan"` | 出口，默认 lan |

实现注意：`register_setting` 的 `secret_fields` 只加密**顶层**字段，endpoint 的
secret 嵌套在列表里，需在 Schema 序列化钩子里用同一加密助手逐项加解密，导出/
读取接口一律打码（`mcwh_****`）。

### 4.2 管理 API

路由文件 `src/movieclaw_api/api/routes/webhook.py`，
`APIRouter(prefix="/webhook", tags=["webhook"])`，统一 `require_login` +
`ApiResponse` 规范：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/webhook` | GET | 读配置（secret 打码） |
| `/webhook` | PUT | 全量更新（新建 endpoint 时响应**一次性**返回 secret 明文，Stripe 惯例） |
| `/webhook/endpoints/{id}/rotate-secret` | POST | 轮换 secret，一次性返回新明文 |
| `/webhook/endpoints/{id}/test` | POST | 发送 `webhook.test` 示例事件，同步返回状态码/耗时/错误 |
| `/webhook/endpoints/{id}/deliveries` | GET | 最近投递记录（内存环形缓冲，每端点 50 条） |

投递记录字段：时间、事件、HTTP 状态码、耗时 ms、尝试次数、错误摘要。P1 存内存
（重启丢失，纯诊断用途），P2 视需求落表支持重放。

### 4.3 设置页 UI

- `apps/web/lib/mock-data.ts` 的「系统」组新增分区
  `{ id: "webhook", label: "Webhook", description: "向外部服务推送播放事件" }`
  （排在「网络与代理」之前）；
- 新组件 `apps/web/components/webhook-section.tsx`，参照
  `im-push-section.tsx` / `downloader-config-section.tsx` 的既有形态：
  - endpoint 列表：名称、URL、格式徽标、启用开关、最近一次投递状态（绿/红点）；
  - 新建/编辑抽屉：URL、格式选择（选 jellyfin 时显示模板输入框 + 变量说明、
    自定义 header；选 movieclaw 时显示 secret 展示/复制/轮换）、事件多选、
    出口选择；
  - 新建成功弹出 secret 明文并提示「仅显示一次」；
  - 每行「发送测试」按钮，展开可见最近投递记录列表。

## 5. 投递器

`src/movieclaw_api/services/webhook/dispatcher.py`：

- 对外唯一入口 `emit_playback_events(events: list[PlaybackEvent]) -> None`：
  同步签名，内部 `create_task`；任务加入模块级强引用集合 `_tasks` 并
  `add_done_callback(discard)`（事件循环只持弱引用，不留强引用推送会无声丢失——
  仓内既有共识）；无事件循环时优雅跳过（供同步测试路径）。
- 任务内：读 `SettingStore` 配置 → 过滤 enabled endpoint 与事件订阅 → 按 format
  调 formatter → `httpx.AsyncClient(timeout=10, transport=egress_transport("webhook", scope))`
  发送 → 失败按 §2.4 重试 → 写环形缓冲。任何异常 `logger.exception` 吞掉，
  日志用中文写明「Webhook 推送失败：<endpoint 名> <原因>」。
- formatter 接口：`format(event) -> (body: bytes, headers: dict)`，P1 实现
  `MovieClawFormatter`，P2 实现 `JellyfinFormatter`。

安全边界：URL 由管理员配置（单管理员产品形态，SSRF 风险等级低），但仍然：
不跟随跨协议/跨主机重定向、响应体最多读 4KB（只为日志摘要）、超时硬上限。

## 6. 分期实施与验收

### P1（自有协议闭环）

| # | 步骤 | 验证 |
| --- | --- | --- |
| 1 | `movieclaw_playback/events.py`：`PlaybackEvent` + 装配器 + ULID 生成 | 新建 `tests/playback/`，单测覆盖电影/剧集/级联/哨兵排除 |
| 2 | 领域函数返回值补 `newly_played` | 既有 `tests/jellyfin/test_protocol_units.py` 不回归 + 新增翻转判定单测 |
| 3 | `settings/webhook.py` Schema + 嵌套 secret 加密 | 单测：落库密文、读取解密、导出打码 |
| 4 | dispatcher + `MovieClawFormatter` + 签名 + 重试 + 环形缓冲；`BUILTIN_EGRESS_SERVICES` 登记 `webhook` | `httpx.MockTransport` 单测：签名可验、重试次数、熔断路径、异常不外抛 |
| 5 | `playstate.py` 6 处埋点（handler 注入 `RequestIdentity`），commit 后 emit | `tests/jellyfin/test_http_flow.py` 扩展：完整播放流断言各事件的产生与去重 |
| 6 | 管理 API 5 个接口 | 路由测试：打码、一次性明文、测试发送 |
| 7 | 前端 `webhook-section.tsx` + 分区注册 | `pnpm web:lint` + `pnpm web:typecheck` |
| 8 | 用户文档（含消费端对接示例：HA、通用 HMAC 校验代码片段） | — |

合并 gate：`pytest`、`ruff check .`、`pnpm web:lint`、`pnpm web:typecheck` 全绿；
手工验收：Infuse 真实播放 → 本地 http 接收端收到 started/stopped/completed，
签名校验通过。

### P2（Jellyfin 兼容 + 增强）

1. 最小 Handlebars 渲染器（验收用例：Yamtrack 与 HA 社区文档里的真实模板）；
2. `JellyfinFormatter`：变量字典、NotificationType 映射、stopped/completed 合并；
3. `playback.progress` 事件 + 按单元 30s 节流；
4. 视反馈：投递日志落表 + 失败重放（届时才需要 Alembic 迁移与 APScheduler 重试任务）。

## 7. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 级联标记整剧产生几十条事件冲击下游 | 逐条发但共享 `batch_id` 供下游聚合；P1 不限流，观察真实反馈 |
| Jellyfin 插件变量集漂移 | 文档声明对齐的插件版本与支持子集，不承诺全集 |
| 下游模板用到未实现的 Handlebars 语法 | 渲染失败计入投递记录并中文报错，按需求迭代助手 |
| 进程重启丢在途投递与记录 | 契约明示"不承诺必达"；P2 落表 |
| secret 嵌套加密遗漏导致明文落库 | 单测强制断言 `app_setting.value_json` 中不出现明文 secret |

## 8. 开放问题

1. 是否增加 `item.favorited` / `item.unfavorited` 事件——触发点就在旁边成本极低，
   待在 issue #86 里询问提议者是否需要。
2. `webhook.test` 事件对 jellyfin 格式端点如何呈现——倾向用示例变量渲染用户模板，
   P2 实现时定稿。
3. `progress` 节流间隔是否需要可配置——P1 不做，P2 按反馈定。
