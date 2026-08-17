# 站点保护与自动刷分享率

PT 站点普遍有上传量 / 分享率考核。一个新接入的站点如果立刻被订阅规则同等对待，
订阅管线会毫无顾忌地从它拉种子——下载量飙高、分享率骤降，轻则被限制下载、
重则封号。本设计提供两个互补机制：

1. **站点保护开关**：打开后该站点不再被订阅链路选中（被动匹配、缺口搜索、
   换源、洗版全部绕开），但用户主动搜索、手动下载完全不受影响。
2. **自动刷分享率（刷流）**：盯着本地种子索引里的免费种子，第一时间抢下做种，
   在用户设定的存储预算内自动汰换上传效率低的、保留高效受欢迎的。

两个机制正交：典型用法是新站「保护 + 刷流」双开，等分享率养起来再关保护。

## 1. 站点保护开关

### 1.1 语义

`SiteCredential.protected`（默认 False）。与 `enabled` / `status` 正交：

| 开关 | 影响面 |
|---|---|
| `enabled=False` | 一切访问停止（搜索、同步、下载） |
| `protected=True` | 只挡**订阅链路**；主动搜索 / 手动下载 / 种子同步照常 |

种子同步（`sync_site_torrents`）**刻意不受保护开关影响**：本地索引是刷流选种
和手动搜索的原料，保护的是"下载量"而不是"浏览页请求"。

### 1.2 实现卡点（两道闸，双保险）

订阅链路触达一个站点只有两条路，各设一道闸：

1. **搜索扇出**：`stream_search_all_sites` 新增 `exclude_protected` 参数，
   订阅侧调用（缺口搜索 `wanted_search`、死种换源 `replacement`）传 True，
   受保护站点直接不发请求（省下的是真实站点压力）。
2. **评估投递**：`evaluate_and_dispatch` 入口过滤受保护站点的种子行。
   这是被动匹配（`sync_site_torrents → process_new_torrents`）的唯一闸门，
   同时兜底任何绕过扇出闸的历史行。

手动链路（`/search/torrents`、手动选种投递 `grab_manual`）不经过这两道闸，
天然不受影响。

## 2. 自动刷分享率

### 2.1 用户视角

站点卡片上一个开关 + 一个预算数字，其余全自动：

- **开关**：`boost_enabled`（默认关）。
- **预算**：`boost_budget_bytes`（默认 100 GiB）。刷流占用的磁盘空间上限，
  引擎在预算内自动汰换。

### 2.2 选种（抢什么）

数据源是本地种子索引 `site_torrent`（同步任务持续前向跟随，新种到达延迟
= 站点自适应同步间隔，5 分钟起）。每 tick 按站点扫描候选，全部条件必须满足：

- `is_free = True`（下载系数 0，下载量零成本——这是刷流不伤分享率的前提）；
- `publish_time` 在 24 小时内（新种的 peer 群最活跃，"第一时间抢"）；
- `leechers ≥ 1`（没有下载者就没有上传对象）；
- `hit_and_run` 不为 True（有 H&R 考核的种子进来容易出不去，绝不碰）；
- `free_deadline` 为 NULL 或剩余窗口 ≥ max(2h, 按 5 MiB/s 估算的下载时长)
  （免费期内下不完就会产生真实下载量，宁可放弃）；
- `size_bytes` 已知且 ≤ 预算的 1/4（单种吃掉大半预算会让汰换失去弹性）。

评分 = `leechers / (seeders + 1)`，再乘上传系数（2x 上传翻倍）。分数高
= 供不应求 = 上传效率预期高。每站每 tick 最多提交 3 个（对站点克制）。

### 2.3 台账与所有权

新表 `ratio_boost_task`（infohash 唯一）记录每个刷流任务。**所有权铁律**：
提交时下载器返回 `already_exists=True`（用户自己已在下）的种子**不入台账、
永不自动删除**——与订阅管线 `owned_by_movieclaw` 同一哲学。

刷流下载与媒体下载彻底隔离：

- 分类 `movieclaw-boost`（订阅/手动是 `movieclaw`），下载器里一眼可辨；
- 保存目录用下载器默认目录下的 `movieclaw-boost/` 子目录，不落媒体库、
  不进监听导入规则的视野，免费种什么内容都有，不能污染入库识别清单。

### 2.4 效率追踪与汰换（留什么）

每 tick 从下载器 `list_torrents` 读一次全量（无逐任务请求），按 infohash
对回台账：

- 累计上传量差分 → **上传速度 EMA**（α=0.3）。EMA 而非瞬时值：PT 上传
  是突发的，瞬时 0 不代表没人要。
- 台账里有、下载器里没有 → 用户手动删了，标记 `missing` 让出预算，不追究。

**汰换规则**（预算不足以容纳新候选、或用户调小了预算时触发）：

- 只汰换同时满足三条的任务：下载已完成、**入池已满 72 小时**、
  上传 EMA < 10 KiB/s；
- 按 EMA 从低到高删（连数据一起删），腾够空间为止；腾不够就放弃新候选，
  **绝不提前动保留期内的任务**。

72 小时最低保留期是 H&R 的安全垫：候选虽然排除了明确标注 H&R 的种子，
但多数站点根本不提供 H&R 标记（三态里的 NULL），72 小时覆盖绝大多数站点
的考核时长要求。宁可预算利用率低一点，不能让"自动刷分享率"反过来制造
H&R 违规。

### 2.5 与订阅/手动下载的碰撞（同一种子两条链路都要）

碰撞是双向的，两个方向都有明确处理：

- **订阅/手动先抢、刷流后到**：两层拦截。准入时排除该站已被
  `subscription_download_attempt` / `manual_download_intent` 认领的种子
  （按 site_id + torrent_id）；即便漏网（如认领记录缺 torrent_id），提交时
  下载器幂等返回 `already_exists`，刷流按所有权铁律不纳入管理。
- **刷流先抢、订阅后到**：订阅投递命中同一 infohash 时下载器幂等返回
  already_exists、工单照常记账（`owned_by_movieclaw=False`，订阅侧不会
  自动删它）。刷流侧每 tick 对账前做**认领转出**（`hand_over_if_claimed`）：
  发现自己的任务 infohash 出现在订阅工单/手动意图里，立即置 missing 让出
  预算——任务与数据原样保留，此后归订阅的所有权/H&R 状态机管辖，刷流的
  汰换永远不会再碰它的数据。文件留在刷流目录（不在库监听视野内），订阅的
  内容核验/换源兜底机制按「下载器中已存在的非自有任务」的既有路径处理。

### 2.6 与既有机制的关系

- 下载器抽象层补 `TorrentBrief.uploaded_bytes / ratio`（qB `uploaded`、
  Transmission `uploadedEver`），两个适配器同步实现；
- 刷流走 `submit_torrent` 公共编排（默认下载器选择、路径映射守门、站点
  限流器全部复用），只是分类与目录不同；
- 定时任务 `ratio_boost`（INTERVAL 300s）注册进现有调度框架；没有任何
  站点开启刷流时 tick 空转直接返回，零成本。

### 2.7 明确不做的

- 不做跨站预算分配策略（每站独立预算，用户直接看得懂）；
- 不做魔力/积分感知（各站规则千差万别，声明式 YAML 覆盖不了）；
- 不做主动限速 / peer 择优（下载器自己的事）；
- 不追偿 `missing` 任务（用户手动删除是明确意图）。

## 3. 数据库变更（向前兼容）

- `site_credential` 加三列（均带 server_default，老代码不认识只丢语义不丢数据）：
  `protected`、`boost_enabled`、`boost_budget_bytes`；
- 新表 `ratio_boost_task`：独立新表，回退旧版本时被忽略。

## 4. API

- `PATCH /sites/{site_id}/protection`：`{"protected": bool}`；
- `PATCH /sites/{site_id}/ratio-boost`：`{"enabled": bool, "budget_bytes": int}`；
- `GET /sites/boost-stats`：按 site_id 返回 `{active_count, used_bytes,
  budget_bytes, uploaded_bytes_total, evicted_count}`，站点卡片展示
  「已用 X / 预算 Y，累计上传 Z」。

三个端点都返回/配合 `ConfiguredSite` 视图新增的 `protected / boost_enabled /
boost_budget_bytes` 字段。
