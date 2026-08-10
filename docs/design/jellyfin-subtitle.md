# Jellyfin 兼容层字幕服务：接口比对与实施设计

> 状态：设计定稿 v1（2026-08-10），待实施。
> 源起：[jellyfin-compat.md](jellyfin-compat.md) §6.5（字幕接口契约）与
> §11 开放问题 1（外挂字幕台账）——两者在 P1 实施时均未落地，本文档
> 是它们的合并展开：接口范围经真 Jellyfin（master，2026-08 克隆）源码
> 逐条比对后定稿，台账/扫描/服务层的落地方案一并给出。
> 关联：[library.md](library.md)（扫描与台账）、
> [strm-workflow.md](strm-workflow.md)（strm 条目旁挂字幕是云播放刚需）。

## 0. 现状盘点（比对基线）

**已有**（与字幕沾边的全部现状）：

- `library_file.subtitle_streams`：ffprobe 探出的**内封**字幕轨 JSON
  （`media_probe._subtitle_stream_info`：codec/language/title/forced/default）；
- `catalog._subtitle_stream()`：内封轨 → MediaStream DTO，恒
  `IsExternal: false`，**无** `DeliveryMethod/DeliveryUrl`；
- PlaybackInfo 不解析 DeviceProfile，恒返回未适配的 MediaSources（等价
  "无转码权限的 Jellyfin"，jellyfin-compat.md §6.1）。

**没有**（本设计要补的）：

1. 任何 `/Videos/*/Subtitles/*` 路由——客户端拿不到外挂字幕，选了也 404；
2. 外挂字幕文件（同目录 `.srt/.ass/...`）的发现与台账——扫描只认
   `SCAN_VIDEO_EXTS + .iso + .strm`，旁挂字幕完全不可见；
3. 字幕流上的 `DeliveryMethod/DeliveryUrl` 输出。

内封字幕在 DirectPlay 下由播放器自行解封装（本地文件与 strm 云直连同理），
**无需服务端参与**——这不是缺口，是分工。真正的缺口只有外挂字幕一条链路。

## 1. 与真 Jellyfin 的接口比对（范围定稿）

真 Jellyfin 字幕相关接口全集（SubtitleController + 关联控制器）逐条裁决：

| 真 Jellyfin 接口 | 用途 | 裁决 | 理由 |
|---|---|---|---|
| `GET /Videos/{itemId}/{msId}/Subtitles/{idx}/Stream.{fmt}` | 外挂字幕按格式输出 | **实现** | 外挂字幕链路的本体 |
| `GET /Videos/{itemId}/{msId}/Subtitles/{idx}/{ticks}/Stream.{fmt}` | 同上，带起始偏移 | **实现**（转调上条，ticks 恒 0） | DeliveryUrl 引用的正是这条；偏移仅转码 seek 场景有意义，我们不转码 |
| `GET /Videos/{itemId}/{msId}/Subtitles/{idx}/subtitles.m3u8` | HLS 字幕子播放列表 | 不做 | 仅 HLS 转码场景（DeliveryMethod=Hls），我们无转码，判定永远不会产生 Hls |
| `GET /Videos/{videoId}/{msId}/Attachments/{index}` | 容器内附件（ASS 字体） | 不做 | 仅服务端转码内嵌/网页 libass 场景；DirectPlay 下播放器从容器自取字体 |
| `GET /FallbackFont/Fonts[/{name}]` | ASS 回退字体 | 不做 | jellyfin-web 的 SubtitlesOctopus 专用 |
| `POST /Videos/{itemId}/Subtitles`（上传） | 字幕管理 | 不做 | UserDto Policy 已宣告 `EnableSubtitleManagement: false`，客户端不亮入口 |
| `DELETE /Videos/{itemId}/Subtitles/{index}` | 字幕管理 | 不做 | 同上 |
| `GET /Items/{itemId}/RemoteSearch/Subtitles/{language}` | 在线字幕搜索 | 不做 | 同上；Infuse/VidHub 的在线字幕匹配是客户端自己的功能，不走服务端 |
| `POST /Items/{itemId}/RemoteSearch/Subtitles/{subtitleId}` | 在线字幕下载 | 不做 | 同上 |
| `GET /Providers/Subtitles/Subtitles/{id}` | 在线字幕预览 | 不做 | 同上 |

除接口外，还有两处**既有响应的增量**（不是新路由）：

- PlaybackInfo 的字幕 MediaStream 补 `DeliveryMethod/DeliveryUrl`（§4）；
- MediaStreams 列表追加外挂字幕流（§4）。

## 2. 外挂字幕台账（前置数据）

### 2.1 落位：`library_file.external_subtitles`（新列，可空 JSON）

与 `subtitle_streams` 同款三态惯例：NULL=未发现过（旧行）、`[]`=发现过但
没有、非空=外挂字幕清单。元素结构：

```json
{"filename": "Movie.2024.chs.default.srt",
 "format": "srt",              // srt/ass/ssa/vtt（小写，即文件扩展名）
 "language": "chi",            // ISO 639-2/B，解析不出为 null
 "title": "chs",               // 语言 token 原文，客户端展示用；无 token 为 null
 "default": false, "forced": false, "sdh": false,
 "size_bytes": 51234, "file_mtime_ns": 1730000000000000000}
```

只存 **basename**：外挂字幕必须与视频文件同目录（Jellyfin 同款约束的
可用子集，不做库级 metadata 目录），服务时拼 `dirname(file_path)`——
整理/搬迁场景视频与字幕一起移动，存全路径反而多一处要维护的一致性。
`size_bytes/file_mtime_ns` 随发现时 stat 落库，服务期做新鲜度校验（改动
过的文件现读现服务，台账下次扫描回填），**浏览场景零文件系统调用**的
硬约束（jellyfin-compat.md §9.5）不被破坏。

迁移向前兼容（发布规范硬约束 3）：纯增列、可空、无回填，旧版本读不到
该列也不受影响。

### 2.2 发现规则（对齐 Jellyfin SubtitleResolver 的可用子集）

同目录下满足 `<video stem>` 前缀 + 字幕扩展名的文件：

```
<stem>.<ext>                    → 无语言单字幕
<stem>.<tokens...>.<ext>        → token 逐段解析
```

- 扩展名 v1 只做**文本字幕**：`srt/ass/ssa/vtt`。`sub/idx`（VobSub 图形
  字幕，需配对文件）与 `sup`（PGS）不做——图形字幕无法转换只能原样外挂，
  主流播放器对外挂图形字幕支持参差，价值/成本比过低；
- token 解析（大小写不敏感，位置无关）：
  - 旗标：`default`、`forced`（含 `foreign`）、`sdh`（含 `cc`、`hi`）；
  - 语言：其余 token 查语言映射表（§6.3），命中即 `language`，多个语言
    token 取第一个；全部不命中时 language=null、title 取整段 token 原文
    （"简中&英文" 这类中文命名照样能选，只是不参与语言归类）；
- strm 条目同样适用：strm 占位文件旁的 `.srt` 一样发现、一样外挂——
  云端媒体 + 本地字幕正是 strm 工作流的常见形态（用户自配中文字幕）。

### 2.3 扫描接入

- 全量/增量扫描：视频文件建行/秒过时顺带 glob 同目录 sidecar（一次
  `iterdir` 已在扫描流程里，逐 stem 前缀匹配是纯内存操作，不新增 IO 轮次），
  变化时更新 `external_subtitles` 列；
- watchdog 实时监控：字幕扩展名事件映射到同目录同 stem 的视频行，触发
  该行的 sidecar 重发现（找不到宿主视频的字幕文件忽略）；
- 探测补探（PROBING 阶段）不管外挂字幕——它只负责 ffprobe 内封信息，
  两套数据源互不牵连。

## 3. 字幕输出接口（协议层新路由）

```
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/{startPositionTicks}/Stream.{format}
GET /Videos/{itemId}/{mediaSourceId}/Subtitles/{index}/Stream.{format}
```

- 带 ticks 版转调不带 ticks 版，ticks 接受并忽略（我们不转码，无 seek
  平移需求；DeliveryUrl 里恒填 0）；
- 每个 route 段有同名 query 可覆盖（`itemId/mediaSourceId/index/format`），
  对齐真 Jellyfin 的 ParameterObsolete 兼容行为；
- 鉴权：`require_device`（偏离③照旧——真 Jellyfin 此接口匿名，我们要求
  token；DeliveryUrl 自带 `?ApiKey=`，客户端媒体内核不带自定义头也能拉）；
- `mediaSourceId` 解析与回落：复用 `_select_source`（小写归一匹配，
  等于 itemId 回落第一个版本——DeliveryUrl 里的 msGuid 必须能反解，
  jellyfin-compat.md §6.2 的既有约束）；
- `index` → 外挂字幕条目：按 §4 的同一套编号算法反推
  （`index - 1 - len(audio_streams) - len(subtitle_streams)` 即
  external_subtitles 下标），越界/非外挂索引 → 404；
- `format` 处理（§6 的服务层实现）：
  - 与源文件同格式（或 `?format=` 显式空串）→ 编码归一后原样输出；
  - `srt → vtt` 支持（未来网页播放器同款需求）；`vtt → srt` 顺带支持
    （pysubs2 同一行代码）；
  - `ass/ssa` 不做跨格式转换：请求 ass 但源是 srt → 404（对齐真 Jellyfin
    "无该格式转换器即失败"的语义；实际不会发生——DeliveryUrl 恒填源格式）；
  - Content-Type：srt→`application/x-subrip`、vtt→`text/vtt`、
    ass→`text/x-ssa`、ssa→`text/x-ssa`（对齐 Jellyfin MimeTypes 表）。

错误形态照本层惯例：GUID 解析失败/无此文件/无此字幕 → 404 空 body。

## 4. MediaStream 与 PlaybackInfo 的增量输出

### 4.1 流编号（决定性算法，两端共用）

现有合成编号不变：video=0、audio 1..n、内封字幕 n+1..m；**外挂字幕接在
最后**：`m+1..k`，顺序 = `external_subtitles` 数组序（对齐真 Jellyfin
"外挂排在内嵌流之后"）。DTO 构建与字幕接口的 index 反解必须走同一个
函数，杜绝两处各算一遍。

已知边界：补探回填会改变 `audio_streams` 长度从而漂移外挂字幕 Index。
接受——真 Jellyfin 重探后同样漂移，客户端每次播放都重新 PlaybackInfo，
单次会话内编号自洽即可。

### 4.2 外挂字幕的 MediaStream（列表/详情/PlaybackInfo 通用）

```json
{"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "chi",
 "IsExternal": true, "SupportsExternalStream": true,
 "IsTextSubtitleStream": true, "IsDefault": false, "IsForced": false,
 "DisplayTitle": "Chinese - SUBRIP - External"}
```

- `Codec` 用 Jellyfin 惯用名：srt→`subrip`、vtt→`webvtt`、ass/ssa 原名；
- `DisplayTitle` 拼接照 §6.3 既有规则，尾缀 ` - External`；language 解析
  不出时用 `title` 原文顶格（"简中&英文 - SUBRIP - External"）。

### 4.3 PlaybackInfo 场景追加投递字段（仅此场景）

```json
 "DeliveryMethod": "External", "IsExternalUrl": false,
 "DeliveryUrl": "/Videos/{itemGuid}/{msGuid}/Subtitles/{idx}/0/Stream.{fmt}?ApiKey=<token>"
```

- **无条件输出**是既定偏离（jellyfin-compat.md §6.1：真 Jellyfin 无
  profile 时不输出，我们输出是外挂字幕可用的必要超集）；`fmt` 恒为源
  格式（Infuse/VidHub 对 srt/ass 全支持，不需要服务端预转换）；
- token 取当前请求的已验证 token（PlaybackInfo 本身过了 `require_device`）；
- 列表/详情（`fields=MediaSources`）**不带**投递字段——对齐真 Jellyfin
  只在 PlaybackInfo 的 SetDeviceSpecificData 里填的行为；
- 内封字幕流照旧不填 DeliveryMethod（DirectPlay 下播放器自行解封装，
  真 Jellyfin 无 profile 时同样不填，此处无偏离）。

## 5. 领域服务层（`movieclaw_playback/subtitles.py`）

按 jellyfin-compat.md §8 的分层：格式转换/编码归一是领域能力（未来网页
播放器复用），协议层只做 GUID 反解与 HTTP 形态。服务函数形态：

```
resolve_subtitle(file: LibraryFile, stream_index: int) -> SubtitleRef | None
    # 编号算法反解出 external_subtitles 条目 + 绝对路径
serve_subtitle(ref: SubtitleRef, out_format: str | None) -> tuple[bytes, str]
    # 读文件 → 编码归一(UTF-8) → 按需转格式 → (字节, content-type)
```

处理流水线（对齐真 Jellyfin SubtitleEncoder 的必要子集）：

1. **编码归一（中文用户的核心价值）**：charset-normalizer 探测，非
   UTF-8/ASCII（GBK/GB18030/BIG5 常见）→ 解码重编 UTF-8。同格式直出也
   过这一步——真 Jellyfin 同款行为，乱码字幕比没字幕更劝退；
2. **格式转换**：pysubs2 解析 → 目标格式序列化（仅 srt↔vtt 矩阵）；
3. **不做磁盘缓存**（相对真 Jellyfin 的有意简化）：外挂文本字幕 <1MB、
   pysubs2 解析毫秒级，现读现转比缓存一致性简单得多；真 Jellyfin 缓存
   主要服务内嵌轨 ffmpeg 抽取（秒级成本），我们没有该场景。台账里的
   `file_mtime_ns` 仅用于探测文件是否被改动过（改过→照常服务新内容，
   下次扫描回填台账）。

## 6. 技术选型（Python 社区比对结论)

### 6.1 字幕解析/转换：pysubs2（定选）

| 候选 | 结论 |
|---|---|
| **pysubs2** | **选它**。MIT、纯 Python、持续维护；SRT/ASS/SSA/WebVTT/MicroDVD/TMP/MPL2 全格式读写，时间轴平移、样式保留齐备——就是 Jellyfin 所用 SubtitleEdit 库的 Python 对应物。`SSAFile.from_string(text).to_string("vtt")` 两行完成转换 |
| srt | 只做 SRT 单格式，纯解析；覆盖面不够 |
| webvtt-py | 只围绕 VTT；srt→vtt 可以但 ass 系不沾 |
| aeidon（Gaupol 内核） | 功能全但 GPL，且以编辑器为中心、依赖重 |

### 6.2 编码探测：charset-normalizer（定选）

| 候选 | 结论 |
|---|---|
| **charset-normalizer** | **选它**。MIT、纯 Python、requests 官方用它替换了 chardet；对 GB18030/BIG5 识别质量好，`from_bytes(...).best()` 一步出结果 |
| chardet | LGPL（许可不如 MIT 干净）、维护放缓 |
| faust-cchardet | C 扩展快但字幕文件 KB 级，性能差异无意义，多背一个二进制轮子 |

解码统一用 `gb18030` 读 GBK/GB2312 探测结果（超集向前兼容），失败退
`errors="replace"` 保底出字——日志按项目惯例输出中文明确报错。

### 6.3 语言 token 映射：不引库，查表

`chs/cht/zh/zh-cn/zh-hans/zh-hant/chi/zho → chi`、`en/eng → eng`、
`ja/jp/jpn → jpn`…… 十几行常量表覆盖实际会出现的命名（catalog 已有
`_LANG_DISPLAY` 展示表，二者相邻放置）。langcodes 库能做完备 BCP-47
解析，但对"文件名后缀猜语言"是牛刀，且引入 language-data 数据包——
按简洁优先原则不上。

### 6.4 内封字幕抽取：v1 不做；做时用 ffmpeg subprocess

DirectPlay 下没有服务端抽取内封字幕的需求。将来若有（如网页播放器要
mkv 内封 srt 转 vtt），沿用 `media_probe` 的直接 `subprocess` 风格调
ffmpeg（`-map 0:{n} -c:s srt`），**不引** ffmpeg-python（已数年无维护）。

### 6.5 依赖清单与发布联动

新增运行时依赖：`pysubs2`、`charset-normalizer`。**实施 PR 必须 bump
`docker/runtime-version`**（发布规范硬约束 2——pyproject dependencies
变更），合并后发新运行时镜像。

## 7. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| S1 | 台账列 + 迁移 + 扫描/watchdog 发现 + 命名解析 | 单测：token 解析矩阵（语言/旗标/中文命名/无 token）；扫描后台账正确，strm 旁挂字幕同样入账 |
| S2 | catalog 输出外挂流 + PlaybackInfo 投递字段 + Stream 接口 + 领域服务 | 单测：编号反解互逆、GBK→UTF-8、srt→vtt 金样；手测：Infuse/VidHub 外挂字幕可选可显，GBK 中文字幕不乱码 |

合并前照例全绿：`pytest`、`ruff check .`、`pnpm web:lint`、
`pnpm web:typecheck`。
