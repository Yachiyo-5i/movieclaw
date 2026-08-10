# movieclaw 字幕支持计划：与 Jellyfin 全链路比对后的完整设计

> 状态：设计定稿 v2（2026-08-10），待实施。v1（同日）只覆盖了接口与台账；
> v2 对照真 Jellyfin（master，2026-08 克隆）把**入库发现**与**播放控制**
> 两条线的细节全部比对后重写，新增播放控制章节（默认轨选择 + 记忆选择），
> 并修正 v1 的一处过时结论（外挂流排序，见 §5.1）。
> 源起：[jellyfin-compat.md](jellyfin-compat.md) §6.5 与 §11 开放问题 1。
> 关联：[library.md](library.md)（扫描与台账）、
> [strm-workflow.md](strm-workflow.md)（strm 旁挂字幕是云播放刚需）。

## 0. Jellyfin 字幕链路全景 × movieclaw 现状

真 Jellyfin 的字幕支持是两条链路九个环节；逐个对照 movieclaw 现状：

**入库线**（发现与台账）：

| # | Jellyfin 环节 | 源码位置 | movieclaw 现状 |
|---|---|---|---|
| 1 | 内封字幕轨探测（ffprobe） | FFProbeVideoInfo | ✅ 已有（`media_probe` → `subtitle_streams` 列） |
| 2 | 外挂字幕发现（同目录前缀匹配） | MediaInfoResolver.GetExternalFiles | ❌ 无——扫描只认视频扩展名 |
| 3 | 文件名 token 解析（语言/旗标/标题） | ExternalPathParser | ❌ 无 |
| 4 | 外挂文件逐个 ffprobe 确认真实编码 | MediaInfoResolver.GetExternalStreamsAsync | ❌ 无 |
| 5 | 库选项过滤与在线字幕自动下载 | AllowEmbeddedSubtitles / SubtitleDownloader | ❌ 无（本计划裁决不做，§2） |

**播放线**（控制与投递）：

| # | Jellyfin 环节 | 源码位置 | movieclaw 现状 |
|---|---|---|---|
| 6 | 默认字幕轨选择（SubtitleMode 五态 × 语言偏好 × 音轨语言） | MediaStreamSelector.GetDefaultSubtitleStreamIndex | ❌ 无——PlaybackInfo 从不输出 DefaultSubtitleStreamIndex |
| 7 | 记忆选择（进度上报的轨序号落库、下次播放优先） | SessionManager + userData.SubtitleStreamIndex | ⚠️ **宣告未兑现**——UserDto 已宣告 `RememberSubtitleSelections: true`，但进度上报的 `SubtitleStreamIndex` 被忽略、不落库 |
| 8 | 投递方式协商（Embed/External/Hls/Encode/Drop） | StreamBuilder.GetSubtitleProfile | ❌ 无（我们不转码 → 只需 External，Embed 由播放器自解） |
| 9 | 字幕输出接口（编码归一 + 格式转换） | SubtitleController + SubtitleEncoder | ❌ 无——一条字幕路由都没有 |

结论：**除环节 1 外全部缺失**，且环节 7 是"对客户端撒了谎"的状态——
宣告了记忆能力，客户端可能因此不在本地记忆，结果两边都不记。

## 1. 目标与不做清单

**目标**（做完后的用户可见效果）：

1. 视频旁的 `.srt/.ass` 外挂字幕在 Infuse/VidHub 里可见、可选、可显示，
   GBK/BIG5 中文字幕不乱码；strm 云播放条目旁挂的本地字幕同样可用；
2. 按文件名旗标与外挂优先规则自动预选默认字幕轨（Default 模式语义）；
3. 用户手动切过的字幕轨被记住，同一条目下次播放自动沿用（含"明确关闭
   字幕"也被记住），兑现 UserDto 既有宣告。

**不做清单**（均有比对依据）：

| 不做项 | 理由 |
|---|---|
| HLS 字幕 m3u8、字幕烧录、Hls/Encode 投递 | 无转码（偏离⑨），判定永远不出这两种方式 |
| Attachments 字体附件、FallbackFont | 仅转码内嵌/网页 libass 场景；DirectPlay 播放器从容器自取字体 |
| 字幕上传/删除/在线搜索下载（SubtitleManagement 五接口 + provider 生态） | Policy 已宣告 `EnableSubtitleManagement: false`，客户端不亮入口；Infuse/VidHub 的在线字幕是客户端自带功能；PT 用户外挂字幕以自备为主 |
| 库选项 AllowEmbeddedSubtitles 四态过滤 | 服务端无转码时内封轨过滤只影响展示列表，价值低；有诉求再议 |
| 外挂图形字幕 `.sub/.idx/.sup`（VobSub/PGS） | 无法转换只能原样外挂，播放器支持参差；`.sub` 还有 MicroDVD 文本歧义（Jellyfin 靠 ffprobe 消歧，见 §3.3 取舍） |
| 外挂音轨/歌词（Jellyfin 同一套 MediaInfoResolver 还管这两类） | 与字幕无关，不搭车 |
| SubtitleMode 五态全量 + 每用户语言偏好 | movieclaw 是单用户模型（设备 token ≠ 多用户），UserDto 恒输出 `SubtitleMode: "Default"`、偏好空串——实现 Default 模式一种即可，语义自洽（§6.2） |

## 2. 要新增/修改的全部接口与行为（定稿清单）

**新路由**（2 条）：

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

**既有响应的增量**（4 处）：

1. MediaStreams 列表（列表/详情/PlaybackInfo 通用）追加外挂字幕流；
2. PlaybackInfo 的外挂字幕流追加 `DeliveryMethod/DeliveryUrl` 等投递字段；
3. PlaybackInfo 的 MediaSource 输出 `DefaultSubtitleStreamIndex`
   （记忆值优先，否则 Default 模式算法；`-1`=用户明确关闭，合法输出）；
4. `/Sessions/Playing` 与 `/Sessions/Playing/Progress` 开始消费
   `AudioStreamIndex/SubtitleStreamIndex` 字段并落库（此前接受并忽略）。

**数据模型**（2 处，均为向前兼容的可空增列）：

- `library_file.external_subtitles`：外挂字幕台账 JSON（§4）；
- `playback_state.audio_stream_index` / `playback_state.subtitle_stream_index`：
  记忆的轨选择（§6.3）。

## 3. 入库线：外挂字幕发现（对照 Jellyfin 逐条取舍）

### 3.1 匹配规则（照抄 MediaInfoResolver.GetExternalFiles）

同目录下，文件名去扩展名后满足：

- 以视频文件的 stem 为**前缀**（OrdinalIgnoreCase）；
- 且要么恰好等于 stem，要么紧跟的下一个字符是分隔符 `.`（Jellyfin 的
  MediaFlagDelimiters 就只有 `.`）。

即 `Movie.mkv` 匹配 `Movie.srt`、`Movie.chs.srt`、`Movie.双语&特效.ass`，
不匹配 `Movie2.srt`。扩展名 v1 收 `srt/ass/ssa/vtt`（Jellyfin 全集还有
`.mks/.sami/.smi/.sub/.sup`，见 §1 不做清单与 §3.3）。

Jellyfin 还会扫条目的 internalMetadataPath（服务端下载字幕的落位目录）；
我们没有在线下载，无此目录，跳过。

### 3.2 文件名 token 解析（对照 ExternalPathParser，含中文场景强化）

对 stem 之后的剩余段按 `.` 分隔，**从右往左**逐 token 判定：

1. 含 `default` → `default=true`，吃掉该 token；
2. 含 `forced` 或 `foreign` → `forced=true`，吃掉；
3. 等于 `cc`/`hi`/`sdh` → `sdh=true`，吃掉（Jellyfin 有 `hi` 与印地语
   撞名的特判：仅当已解析出语言 hin 时才把 hi 当听障旗标——我们语言表
   不收 `hi` 这个歧义码，直接规避，见 §7.3）；
4. 语言映射表命中 → `language`（首个命中生效），吃掉；
5. 都不是 → 拼入 `title`（保持原顺序）。

与 Jellyfin 的差异（有意）：它用 `Contains` 匹配旗标（`xxdefaultxx` 也
命中），我们用**整段相等**——Contains 会把中文命名里的巧合串误判，
宽松无收益。

### 3.3 编码确认：v1 信任扩展名，不 ffprobe（有意简化）

Jellyfin 对每个外挂字幕文件跑一次 ffprobe 拿真实 codec（防扩展名说谎，
也为 `.sub` 的 MicroDVD/VobSub 歧义消歧）。我们 v1 不做：

- 收的四种扩展名（srt/ass/ssa/vtt）语义明确，说谎场景罕见；
- 不收歧义的 `.sub`，消歧需求不存在；
- 服务期 pysubs2 解析失败会以 404+中文日志显性暴露，不会静默错显；
- 省掉扫描期逐字幕文件的子进程开销（云盘挂载上尤其可观）。

留一个显式验证点：S1 验收里加"扩展名与内容不符的坏文件 → 服务期报错
日志可读"的用例。

### 3.4 扫描与监控接入

- 全量/增量扫描：视频行建立/秒过时顺带 sidecar 匹配（目录列表已在扫描
  流程内存中，前缀匹配零额外 IO 轮次）；结果含 `size_bytes/file_mtime_ns`
  （发现时 stat 一次），变更才写列；
- watchdog：字幕扩展名的文件事件 → 映射到同目录同前缀的视频行 → 重发现
  该行 sidecar（找不到宿主视频的字幕事件忽略）；
- 探测补探（PROBING）不管外挂字幕——内封/外挂两套数据源互不牵连。

## 4. 台账：`library_file.external_subtitles`（新列，可空 JSON）

与 `subtitle_streams` 同款三态惯例：NULL=未发现过（旧行，重扫回填）、
`[]`=发现过但没有、非空=清单。元素结构：

```json
{"filename": "Movie.2024.chs.default.srt",
 "format": "srt",              // srt/ass/ssa/vtt（小写=扩展名）
 "language": "chi",            // ISO 639-2/B；解析不出为 null
 "title": "chs",               // 未消费 token 原文；无则 null
 "default": false, "forced": false, "sdh": false,
 "size_bytes": 51234, "file_mtime_ns": 1730000000000000000}
```

只存 **basename**：外挂字幕必须与视频同目录（Jellyfin 约束的可用子集，
不做 metadata 目录），服务时拼 `dirname(file_path)`——整理/搬迁时视频
与字幕一起移动，存全路径反而多一处一致性要维护。`size_bytes/file_mtime_ns`
仅为服务期新鲜度探测（改过→现读现服务新内容，下次扫描回填台账），
**浏览零媒体 IO** 硬约束（jellyfin-compat.md §9.5）不被破坏。

迁移向前兼容（发布规范硬约束 3）：纯增列、可空、无回填。

## 5. 播放线 A：流声明与投递

### 5.1 流编号（决定性算法，DTO 与字幕接口共用一个函数）

现有合成编号不变：video=0、audio 1..n、内封字幕 n+1..m；**外挂字幕接在
最后** m+1..k，顺序=台账数组序。

**记录一处与 Jellyfin master 的有意偏离**（v1 结论过时，此处修正）：
master 现把外挂流插在容器流**之前**再统一重编号（FFProbeVideoInfo 注释：
为远程视频保持流 ID 稳定），10.x 旧版才是外挂在后。我们维持外挂在后：
增删 sidecar 不漂移内封编号（补探回填场景更稳），且客户端只认服务端
下发的 Index 值、不做位置假设，两种排法协议上等价。

已知边界照旧：补探回填改变音轨数会漂移外挂 Index；客户端每次播放都重新
PlaybackInfo，单次会话内自洽即可（真 Jellyfin 重探后同样漂移）。

### 5.2 外挂字幕的 MediaStream（列表/详情/PlaybackInfo 通用）

```json
{"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "chi",
 "IsExternal": true, "SupportsExternalStream": true,
 "IsTextSubtitleStream": true, "IsDefault": false, "IsForced": false,
 "IsHearingImpaired": false, "DisplayTitle": "Chinese - SUBRIP - External"}
```

- `Codec`：srt→`subrip`、vtt→`webvtt`、ass/ssa 原名（Jellyfin 惯用名）；
- 旗标取台账 default/forced/sdh；`DisplayTitle` 照 §6.3 既有拼接规则加
  ` - External` 尾缀，language 解析不出时用 title 原文顶格
  （`"简中&英文 - SUBRIP - External"`）。

### 5.3 PlaybackInfo 场景追加投递字段（仅此场景）

```json
 "DeliveryMethod": "External", "IsExternalUrl": false,
 "DeliveryUrl": "/Videos/{itemGuid}/{msGuid}/Subtitles/{idx}/0/Stream.{fmt}?ApiKey=<token>"
```

- **无条件输出**是既定偏离（真 Jellyfin 无 DeviceProfile 不输出；我们
  不解析 profile，无条件输出是外挂字幕可用的必要超集）；`fmt` 恒为源
  格式（Infuse/VidHub 对 srt/ass 全支持，无需预转换）；
- token 取当前请求已验证的 token；列表/详情（`fields=MediaSources`）
  **不带**投递字段（对齐真 Jellyfin 只在 PlaybackInfo 填充的行为）；
- 内封字幕流照旧不填 DeliveryMethod（DirectPlay 播放器自解，真 Jellyfin
  无 profile 时同样不填，此处无偏离）。

## 6. 播放线 B：默认轨选择与记忆选择（新增章节）

### 6.1 Jellyfin 的完整行为（比对基线）

PlaybackInfo 时 `SetDefaultAudioAndSubtitleStreamIndices`：

1. **记忆优先**：`RememberSubtitleSelections` 开启且 userData 存有
   `SubtitleStreamIndex` → 校验该索引仍存在（或为 `-1`="明确关字幕"）
   后直接输出；
2. 否则跑 `MediaStreamSelector.GetDefaultSubtitleStreamIndex`：候选排序
   `IsExternal ↓ → IsDefault ↓ → 非forced且语言命中 ↓ → forced且语言命中 ↓
   → forced且语言未定义 ↓ → forced ↓`，再按 SubtitleMode 五态过滤
   （None/Default/Smart/Always/OnlyForced，Smart/Always 还要看当前音轨
   语言与偏好语言的关系）；
3. **记忆写入**：Start/Progress 上报里的 `SubtitleStreamIndex` 落
   userData（开关关闭时反向清空已存值）。

### 6.2 movieclaw 实现：Default 模式 + 记忆（单用户模型的忠实子集）

UserDto 恒输出 `SubtitleMode: "Default"`、`SubtitleLanguagePreference: ""`、
`RememberSubtitleSelections: true`，实现与宣告对齐：

- **选择算法**：只实现 Default 模式（宣告值），语言偏好空串=通配。
  排序照抄（通配下简化为 `IsExternal ↓ → IsDefault ↓ → IsForced ↓`，
  内部次序保持 Index 升序稳定），过滤条件
  `IsExternal || IsDefault || IsForced`，取首个；全不命中 → 省略字段
  （客户端默认不开字幕）。效果：有外挂字幕就预选外挂（中文用户装了
  字幕就是要看的），否则尊重内封 default/forced 旗标；
- **音轨对应项**：`DefaultAudioStreamIndex` 现状已输出（default 旗标
  优先、否则第一条音轨），补上记忆优先级即可，选择算法不动。

### 6.3 记忆落库：`playback_state` 增两列

- `audio_stream_index: int | None`、`subtitle_stream_index: int | None`
  （NULL=从未上报过；字幕列的 `-1`=用户明确关闭，照 Jellyfin 语义）；
- 写入：`/Sessions/Playing`（开始）与 `/Sessions/Playing/Progress` 消费
  这两个字段，值变化才写（Progress 高频，避免每次 UPDATE）；`Failed=true`
  的 Stopped 照既有规则整体跳过；
- 读出：PlaybackInfo 按 §6.1 优先级输出，索引失效（重扫后轨变了）回落
  选择算法——**校验必须做**，否则客户端拿到悬空索引行为未定义；
- movieclaw 是单用户模型：记忆按 (条目, 季, 集) 全局生效，无 per-user
  维度，与 playback_state 既有语义一致。

## 7. 字幕输出接口与领域服务

### 7.1 路由（协议层）

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

- 带 ticks 版转调不带 ticks 版，ticks 接受并忽略（不转码无 seek 平移；
  DeliveryUrl 恒填 0）；route 段有同名 query 可覆盖（itemId/
  mediaSourceId/index/format，对齐 ParameterObsolete 兼容行为）；
- 鉴权 `require_device`（偏离③照旧：真 Jellyfin 此接口匿名，我们要求
  token，DeliveryUrl 自带 `?ApiKey=`）；
- `mediaSourceId` 复用 `_select_source`（小写归一匹配 + 等于 itemId 回落
  第一个版本——DeliveryUrl 里的 msGuid 必须能反解）；
- `index` 按 §5.1 同一函数反推台账下标，越界/指到内封轨 → 404；
- `format`：与源同格式（或 `?format=` 显式空串）→ 编码归一后原样输出；
  `srt↔vtt` 互转；ass/ssa 不跨格式转换，请求了给 404（对齐真 Jellyfin
  "无转换器即失败"；实际不会发生——DeliveryUrl 恒填源格式）；
- Content-Type：srt→`application/x-subrip`、vtt→`text/vtt`、
  ass/ssa→`text/x-ssa`（对齐 Jellyfin MimeTypes）；
- 错误形态照本层惯例：404 空 body；日志中文说明具体原因（找不到文件/
  解析失败/编码不明），非开发者可读。

### 7.2 领域服务（`movieclaw_playback/subtitles.py`）

按 jellyfin-compat.md §8 分层：转换/编码归一是领域能力（未来网页播放器
复用 srt→vtt 同一函数），协议层只做 GUID 反解与 HTTP 形态。

```
resolve_subtitle(file: LibraryFile, stream_index: int) -> SubtitleRef | None
serve_subtitle(ref: SubtitleRef, out_format: str | None) -> tuple[bytes, str]
```

流水线（对齐 SubtitleEncoder 的必要子集）：

1. **编码归一（中文用户的核心价值）**：charset-normalizer 探测，非
   UTF-8/ASCII（GBK/GB18030/BIG5 常见）→ 解码重编 UTF-8；同格式直出也
   过这一步——真 Jellyfin 同款行为，乱码字幕比没字幕更劝退；
2. **格式转换**：pysubs2 解析 → 目标格式序列化（仅 srt↔vtt）；
3. **不做磁盘缓存**（相对真 Jellyfin 的有意简化）：外挂文本字幕 <1MB、
   解析毫秒级，现读现转比缓存一致性简单；真 Jellyfin 的缓存主要服务
   内封轨 ffmpeg 抽取（秒级成本），我们没有该场景。

### 7.3 技术选型（Python 社区比对结论）

| 模块 | 选型 | 理由与备选 |
|---|---|---|
| 格式解析/转换 | **pysubs2** | MIT、纯 Python、活跃维护；SRT/ASS/SSA/VTT/MicroDVD 全读写、时间轴平移齐备——SubtitleEdit 的 Python 对应物。备选：srt（单格式）、webvtt-py（只围绕 vtt）、aeidon（GPL 且依赖重） |
| 编码探测 | **charset-normalizer** | MIT、纯 Python，requests 官方以它替换 chardet（LGPL、维护放缓）；GB18030/BIG5 识别好。cchardet 系 C 扩展对 KB 级文件无意义。GBK 探测结果统一按 `gb18030` 超集解码，失败退 `errors="replace"` 保底出字 |
| 语言 token 映射 | 不引库，查表 | `chs/cht/zh/zh-cn/zh-hans/zh-hant/chi/zho→chi`、`en/eng→eng`、`ja/jp/jpn→jpn` 等十几行常量（与 catalog `_LANG_DISPLAY` 相邻放置）。**不收 `hi`**（印地语/听障旗标歧义，Jellyfin 为此写了特判，我们直接规避）。langcodes 库对"文件名猜语言"过重 |
| 内封轨抽取 | v1 不做 | DirectPlay 无此需求；将来（网页播放器要 mkv 内封转 vtt）沿用 media_probe 的 subprocess 风格调 ffmpeg，不引 ffmpeg-python（多年无维护） |

### 7.4 依赖与发布联动

新增运行时依赖：`pysubs2`、`charset-normalizer`。**实施 PR 必须 bump
`docker/runtime-version`**（发布规范硬约束 2），合并后发新运行时镜像。

## 8. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| S1 | 台账列 + 迁移 + 扫描/watchdog 发现 + 命名解析 | 单测：前缀匹配边界（stem 相等/带分隔符/前缀撞名不误收）、token 矩阵（语言/旗标/中文命名/无 token/从右往左顺序）；扫描后台账正确，strm 旁挂同样入账 |
| S2 | 外挂流 DTO + PlaybackInfo 投递字段 + Stream 接口 + 领域服务 | 单测：编号反解互逆、GBK→UTF-8、srt→vtt 金样、坏文件 404+中文日志；手测：Infuse/VidHub 外挂字幕可选可显、GBK 不乱码 |
| S3 | playback_state 增列 + 进度上报消费轨字段 + DefaultSubtitleStreamIndex（记忆优先 + Default 算法，含 -1 语义与失效回落） | 单测：选择算法排序矩阵、记忆读写与失效回落、-1 往返；手测：切轨后重进沿用、关字幕后重进仍关 |

S1/S2 一起交付才有用户可见价值（台账没有接口是无米之炊的反面）；S3 可
独立后行。合并前照例全绿：`pytest`、`ruff check .`、`pnpm web:lint`、
`pnpm web:typecheck`。
