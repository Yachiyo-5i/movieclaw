# Sonarr / Radarr 洗版机制调研报告

> 状态：调研完成（2026-08）。本文是 `subscription.md` 3.3 节决策层预留的"P6 洗版"
> 扩展点的前置调研：深度拆解 Sonarr/Radarr 的质量升级（quality upgrade，即"洗版"）
> 设计，归纳其优缺点与真实用户反馈，并对比周边生态与中文社区方案
> （MoviePilot / nas-tools），最后落到对 movieclaw 的设计启示。
>
> 信息源：Servarr 官方 Wiki、TRaSH Guides、Radarr/Sonarr `develop` 分支源码与
> GitHub issue、Reddit（经 Arctic Shift 存档 API）、官方论坛、Hacker News、Lemmy、
> 中文社区（MoviePilot/nas-tools wiki 与源码、博客）。用户反馈均为逐字原文引用，
> 附来源 URL。

---

## 1. 机制全景：Sonarr/Radarr 怎么做洗版

### 1.1 两层模型：Quality Profile × Custom Formats

Sonarr/Radarr 的洗版建立在两个正交维度上：

**质量维度（Quality Profile）**：
- 质量（Bluray-1080p、WEBDL-1080p、HDTV-720p…）在 profile 里**按列表位置排序**，
  未勾选的质量也参与排序比较（用于给已有文件定位），但只有勾选的才会被抓取；
- **组（group）内的质量视为相等**——典型如 WebDL 与 WebRip 合并为 "WEB" 组。
  这个"组内相等"后来成为社区打破质量霸权的钥匙（见 1.3）；
- **Upgrades Allowed**：升级总开关；
- **Upgrade Until（quality cutoff）**：达到该质量后不再做质量维度升级。

**打分维度（Custom Formats，下称 CF）**：
- 一个 CF = 一组条件（Release Title 正则、Source、Resolution、Language、
  Indexer Flag、Size 区间、Release Group…）+ 每个 profile 独立赋予的分数；
- 同类型多条件为 OR（可用 Required 提升为必须），不同类型条件之间为 AND，
  条件可 Negate 取反；
- 一个 release 的 CF 总分 = 所有命中 CF 的分数之和，分数可为负；
- 三个阈值：**Minimum Custom Format Score**（低于即拒绝的硬门槛）、
  **Upgrade Until Custom Format Score**（现有文件达到此分后停止 CF 升级）、
  **Minimum Custom Format Score Increment**（新分数须比现有分数至少高出此步长
  才算升级，2024 年新增的防抖字段）；
- CF 即时计算不落库，改定义立刻对全库生效；CF 不影响搜索请求，只影响结果评估。

来源：[Radarr Settings](https://wiki.servarr.com/radarr/settings)、
[Sonarr Settings](https://wiki.servarr.com/sonarr/settings)、
[TRaSH Quality Profiles](https://trash-guides.info/Radarr/radarr-setup-quality-profiles/)。

### 1.2 升级判定状态机（源码级）

`UpgradableSpecification.IsUpgradable()`（Radarr/Sonarr 同构）对"候选 release vs
现有文件"的判定顺序，是理解整个洗版行为的钥匙：

```
1. 质量更高 且 质量 cutoff 未满足        → 接受（质量升级，此时不看 CF 分数）
2. 质量更低                              → 拒绝 BetterQuality
3. 质量相同，revision（Proper/Repack）更高 → 接受；更低 → 拒绝 BetterRevision
   （仅当 "Download Propers and Repacks" ≠ Do Not Prefer 时参与比较）
4. Upgrades Allowed 未开                 → 拒绝 UpgradesNotAllowed
5. 质量更高但质量 cutoff 已满足           → 拒绝 QualityCutoff
6. 质量相等，进入 CF 分数比较：
   新分 ≤ 现有分                         → 拒绝 CustomFormatScore
   现有分 ≥ Upgrade Until CF Score       → 拒绝 CustomFormatCutoff
   新分 < 现有分 + Score Increment       → 拒绝 MinCustomFormatScore
   否则                                  → 接受（CF 升级）
```

三个关键语义：

- **质量严格优先（"Quality Trumps All"）**：CF 分数只在质量相等时驱动升级；
  质量升级也不要求 CF 分数提升（但仍须过 Minimum CF Score 硬门槛）。
- **质量 cutoff 满足后，CF 升级仍会继续**——直到 CF 分数达到 Upgrade Until CF
  Score。"整体 cutoff 已满足"的定义是两个 cutoff 同时满足，任一未满足条目就
  留在 Cutoff Unmet 列表里。这是用户困惑的最大来源（见第 3 节主题 D）。
- **每个拒绝都有结构化理由**（BetterQuality / QualityCutoff / CustomFormatCutoff
  / MinCustomFormatScore…），interactive search 界面直接展示。

多个可接受候选之间的排序键（Radarr）：Quality → CF Score → Protocol →
Indexer Priority → Indexer Flags → Seeds/Peers → Age → Size（接近 preferred
size 者优）。Sonarr 在 CF Score 后多一级 Episode Count（整季包优先）。

来源：[UpgradableSpecification.cs](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/UpgradableSpecification.cs)、
[DownloadDecisionComparer.cs](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/DecisionEngine/DownloadDecisionComparer.cs)、
[Radarr FAQ](https://wiki.servarr.com/radarr/faq)。

### 1.3 想让 CF 压过质量？官方不给开关，给 hack

用户的真实偏好常常是"CF 优先于分辨率"（例：宁要 1080p HDR 不要 2160p SDR，
宁要母语配音的低清也不要英语高清——[Radarr #4666](https://github.com/Radarr/Radarr/issues/4666)，
被官方关闭）。官方的答案不是加开关，而是 TRaSH 的 **merge-quality 技巧**：把
多个质量合并进同一个组使其"相等"，于是比较全部落到 CF 分数上。整个决策序
硬编码不可配置，表达"我的优先级"只能靠这种结构改写。

### 1.4 触发渠道：常态靠 RSS 推流，主动搜索永远是显式的

官方 FAQ 的核心设计声明：

> "Radarr does *not* regularly search for movie files that are missing or have
> not met their quality goals. Instead, it fairly frequently queries your
> indexers and trackers for *all* the newly posted movies, then compares that
> with its list of movies that are missing or need to be upgraded."

- **常态通道是 RSS Sync（推模式）**：15-60 分钟间隔拉取索引器"新发布"流，与
  "缺失或待升级"清单比对，命中即抓。任意规模的库每天只需几十次查询；
- **没有内置的周期性升级搜索任务**。主动搜索只由用户显式触发：
  Wanted → Missing / **Cutoff Unmet** 列表的手动批量搜索、单条目的
  Automatic / Interactive Search、添加条目时的 Start search；
- 官方甚至推荐第三方脚本 Upgradinatorr 做"大改 profile 后的批量洗版搜索"——
  刻意不内置全库轮询，以控制索引器 API 成本。

这与 movieclaw `subscription.md` 2.4 节"被动匹配为主通道、主动搜索限流兜底"
的结构高度同构。

来源：[Radarr FAQ](https://wiki.servarr.com/radarr/faq)、
[Sonarr Wanted](https://wiki.servarr.com/sonarr/wanted)。

### 1.5 升级落地：文件替换与做种解耦

- **旧文件**：导入升级文件时删除旧媒体文件；配置了 Recycling Bin 则进回收站
  （按天数自动清理）——这是误洗回退的唯一保险；
- **History** 记录 grab/import/delete/upgrade 全部事件，且是决策输入
  （防止重复抓取已抓过且不更优的 release）；
- **下载客户端里的旧种子不动**：导入用 hardlink/copy，删媒体库文件不影响种子
  文件本体，私有站做种不受升级影响；旧种最终由 Completed Download Handling
  在"客户端报告做种达标并暂停"后才删除。

来源：[Radarr FAQ "Why are there two files?"](https://wiki.servarr.com/radarr/faq)、
[Sonarr FAQ "Seeding torrents aren't deleted automatically"](https://wiki.servarr.com/sonarr/faq)。

### 1.6 Sonarr 特有：按集粒度与整季包

- Quality Profile 挂在剧上，但**升级决策与文件跟踪都是集级**；
- 整季包在"所有集播出"之前一律拒绝（`FullSeasonSpecification`）；
- **整季包 vs 已有单集的冲突处理**：对整季包映射到的每一集已有文件逐个跑
  1.2 节状态机，**任何一集判 reject 就拒绝整个包**——不存在"为洗 3 集重下
  整季、顺带把另外 7 集洗成平级"的情况；反之某一集已达 cutoff 也会挡住整季包。

来源：[UpgradeDiskSpecification.cs](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/UpgradeDiskSpecification.cs)、
[FullSeasonSpecification.cs](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/FullSeasonSpecification.cs)。

### 1.7 十年演化史：布尔开关不断被打分收编

| 时代 | 机制 | 问题 |
|---|---|---|
| Radarr v2 / Sonarr v3 前 | Restrictions（must/must not contain） | 只能过滤，无偏好表达 |
| Sonarr v3 | Release Profiles / Preferred Words（正则+全局分数） | 分数全局共享、**没有分数 cutoff、永远升级**、不受 Upgrades 开关约束 |
| Radarr v3 (2020) | Custom Formats 初版 | CF 分数按 profile 隔离 |
| Sonarr v4 (2023) | 移除 Preferred Words，全面迁移 CF | 迁移"tricky"，每行 preferred word 机械变成一个碎片 CF |
| 2024 | Minimum CF Score Increment | 修打分体系自身引入的"+1 分洗整部"抖动 |

官方给出的迁移理由（Sonarr v4 FAQ 原文）正是洗版设计的两条核心教训：

1. 粒度："custom formats can be given different levels of importance **for
   each quality profile**"（分数按 profile 隔离）；
2. 可收敛："Custom Formats can also be given a **cutoff level so that upgrades
   stop happening**… whereas the old preferred words system **upgraded always**"
   （升级必须有终点）。

同方向的收编还有：语言 profile → Language CF；Propers/Repacks 全局布尔 →
Repack/Proper CF（官方 FAQ 以 warning 形式推荐，因为内置 revision 比较发生在
CF 之前且无条件压制 CF——你打了低分的组发个 Repack 也会强制洗掉你打高分的
版本）；整季包偏好 → Season Pack CF；freeleech → Indexer Flag CF。

来源：[Sonarr v4 FAQ](https://wiki.servarr.com/sonarr/faq-v4)、
[Radarr #9994](https://github.com/Radarr/Radarr/issues/9994)、
[Sonarr #6800](https://github.com/Sonarr/Sonarr/issues/6800)。

---

## 2. 设计优点（含用户好评实证)

### 2.1 "先看后洗"是被用户点名喜爱的核心场景

首播当天先抓一版能看的（HDTV/WEB），后台自动洗到 WEB-DL/蓝光终点。这个场景
从 2015 年起就被反复正面提及：

> "I have mine set to grab HDTV-720 releases first and then let it grab WEB-DL
> or BluRay releases as it finds them. So far I've seen this process work as
> expected"
> —— [forums.sonarr.tv, 2015](https://forums.sonarr.tv/t/episode-quality-update-schedule/4345)

> "I love the cutoff feature, I think it's pretty great. … I'll want the cutoff
> to be BluRay because that's the ultimate target."
> —— [forums.sonarr.tv, 2017](https://forums.sonarr.tv/t/if-cutoff-is-unmet-only-upgrade-if-at-least-cutoff-quality-is-found/13320)

### 2.2 CF 打分的表达力：从"选档位"到"手工挑版级精度"

> "setting up custom formats was a game changer for me. It will only download
> releases I would pick myself based on a bunch of factors (prefer special
> edition / unrated, release group, gb/hour, format, provider, network, etc…)
> I used to always search manually cause the 'quality profile' just wasn't
> specific enough."
> —— [r/radarr, 2023](https://www.reddit.com/r/radarr/comments/10i5bnx/first_time_using_radarr_but_have_a_weird_question/j5d5jj4/)

> "These are fantastic have really upped my quality. I was just randomly
> grabbing releases without knowing whether they had atmos or hdr 10 or dv etc.
> This is SUPER helpful as you can give a score and then when you manually
> search, it chooses the highest score."
> —— [Lemmy, 2023](https://sh.itjust.works/comment/596368)（22 赞）

CF 还未进入 Sonarr 时，Sonarr 用户主动请愿移植（"With just some basic
knowledge, users can configure every special case they want with it"，
[forums.sonarr.tv, 2018](https://forums.sonarr.tv/t/custom-formats-like-radarr/20663)）；
v4 落地后有用户因"CF 可按质量档位分别打分"而砍掉了专门为动漫分开跑的第二个
实例（[forums.sonarr.tv, 2023](https://forums.sonarr.tv/t/friendlier-mobile-ui-for-select-series-option/31851)）。

### 2.3 自动化省心 + 生态加成

"Set it and forget it"是泛化好评的主旋律（"Been going strong with very little
maintenance for 6 years"，[Lemmy, 2024](https://lemmy.ml/comment/11128980)），
且升级/失败重试被用户当作省心的组成部分（"It also handles failures,
redownloads, quality upgrades"，[HN, 2023](https://news.ycombinator.com/item?id=35844696)）。
CF 以 JSON 导入导出 + trash_id 引用，配置成为可分享资产，TRaSH/Recyclarr/
Profilarr 生态反过来成为用户留在 arr 系的理由（"Recyclarr is very important
for me because I am assured to request only the very best quality"，
[Lemmy, 2023](https://lemmy.world/comment/4982553)）。

### 2.4 设计上值得抄的四个决定

1. **升级有终点且防抖**：quality cutoff、CF score cutoff、score increment
   三个停止条件，全部是被真实事故逼出来的（preferred words 无限升级、
   freeleech +1 分洗整部）；
2. **常态推流比对 + 显式主动搜索**，不做全库轮询，索引器成本可控；
3. **升级与做种解耦**（hardlink + 删库不删种 + 达标后删种），私有站友好；
4. **拒绝理由结构化**，每个 reject 可解释、可在 UI 展示。

---

## 3. 设计缺点与真实差评

调研收集了 20+ 条一手负面反馈（GitHub issues、官方论坛、Reddit、Lemmy、HN），
归纳为 8 个主题。每个主题附代表性原话与官方回应情况。

### 主题 A：配置复杂度与学习曲线陡峭

表达"我想要什么版本"需要同时理解 quality 序、cutoff、CF 正负打分、min score、
upgrade-until score、increment、delay profile 等 6+ 个正交概念：

> "However, custom formats are incredibly confusing to me."
> —— [r/radarr, 2020](https://reddit.com/r/radarr/comments/jegeay/custom_filters_for_rss_and_search/)

> "Are people just using one of the built-in quality profiles at pretty much
> random? I chose 2160p Efficient but i dont even really know what that means…
> Making your own quality profile seems just daunting to me."
> —— 用了多年 arr 的老用户，[r/selfhosted, 2026](https://reddit.com/r/selfhosted/comments/1tq2bqu/feeling_overwhelmed_with_profilarr/)

非英语内容（多语言/双音轨）是复杂度重灾区；为控制体积写打分再叠加 delay
profile 后"行为完全不可预测"（["Kind of ripping my hair out", r/radarr, 2025](https://reddit.com/r/radarr/comments/1onrus3/kind_of_ripping_my_hair_out_with_delay_profile/)）。
**官方基本无回应**：立场是提供机制不提供策略，复杂度外包给 TRaSH。

### 主题 B：生态依赖——不读 TRaSH 配不好，默认配置视同不可用

> "According to Trash Guides, it sounds like you should basically erase and
> ignore all of the default settings… Is there something seriously broken about
> the default Qualities in Radarr/Sonarr that necessitates this step?"
> —— [r/radarr, 2024](https://reddit.com/r/radarr/comments/1dycjci/can_someone_explain_the_purposeuse_of_custom/)

> "I spent a half hour looking at them, then another half hour giving that
> stuff ago and threw my hands up in the air and went back to Plex."
> —— [HN, 2026](https://news.ycombinator.com/item?id=48109145)

要达到"下到最优版本"，用户需按 TRaSH 指南走 10-15 步、导入 50-70 个 CF 并逐一
填分——CF 的 JSON 导入导出能力甚至是社区为分发指南才向官方提请加上的。
官方默许 TRaSH 为准官方文档（wiki 多处链接），未内置任何推荐 profile 一键导入。

### 主题 C：升级决策不透明——"为什么抓/为什么不抓/为什么又抓"

> "If upgrades allowed is disabled and radarr downloads a file with let's say a
> custom format of 20, and then it finds another file with a higher custom
> format, will it be downloaded even if upgrades allowed is disabled?"
> —— 连问三个组合行为问题且"找不到文档能回答"，[r/radarr, 2024](https://reddit.com/r/radarr/comments/1c8ue6n/how_do_upgrades_and_custom_formats_really_work/)

决策序硬编码（quality 永远优先）与用户直觉相反的诉求被关闭
（[#4666](https://github.com/Radarr/Radarr/issues/4666)），只有 merge-quality hack。

### 主题 D：双轨 cutoff 语义分裂（最大共性痛点）

quality cutoff 和 CF score cutoff 是两条独立的停止线，开关、门槛、Wanted 列表
各自为政，用户持续以 bug 心智提交 issue，官方多次以 by design 拒绝：

- 关掉 Upgrades Allowed 后 CF 升级照跑，且相关字段在 UI 里被隐藏，"extra
  unintuitive"（[Sonarr #5330](https://github.com/Sonarr/Sonarr/issues/5330)，
  **这条修了**：v4 起总开关同时约束 CF 升级）；
- CF 升级无视 quality cutoff 抓 Remux（"the quality cutoff which in theory
  should trump everything"，[Radarr #8041](https://github.com/Radarr/Radarr/issues/8041)，
  closed as unlikely——1.2 节状态机第 6 步就是这个行为，官方认定是设计）；
- Minimum CF Score 被更高质量绕过：1080p 双语 +3 分被 2160p 单语 -3 分顶替
  （[Sonarr #7634](https://github.com/Sonarr/Sonarr/issues/7634)，closed as
  not planned）;
- Cutoff Unmet 列表与实际升级行为不一致，双向都有困惑：不会被升级的 720p
  出现在列表里（[r/radarr, 2024](https://reddit.com/r/radarr/comments/1eb2x6u/)；
  [Radarr #6924](https://github.com/Radarr/Radarr/issues/6924) 开了四年未动）；
  仅 CF 分数未达标的影片反而**不**出现在列表里
  （[r/radarr, 2025](https://reddit.com/r/radarr/comments/1j05yrw/)）。

### 主题 E：资源浪费（磁盘/带宽/意外巨型文件）

> "Just downloaded a 44gb file for a 1080p version of Forest Gump… for 1080p I
> don't need it to be any bigger than 3gb"
> —— [Lemmy, 2024](https://lemmy.world/post/10124813)

freeleech +1 分导致 1000 分与 1001 分互相"升级"、整部重下——官方 2024 年补
Minimum CF Score Increment 才治好（[Radarr #9994](https://github.com/Radarr/Radarr/issues/9994)）。
体积控制至今仍需用户自配 Quality Definitions 上限或体积类 CF。

### 主题 F：升级循环（结构性 bug，至今未根治）

抓取时按**种子名**打分、导入时按**文件名**重新打分，两次结果不一致即死循环：

> "Notice the score being lower after import. Search for this media again…
> The exact same release is grabbed again. Repeat."
> —— [Radarr #11422, 2026-04](https://github.com/Radarr/Radarr/issues/11422)，仍 open

另一种表现：反复抓 50GB remux、导入时又判定不是升级而拒收，seedbox 流量白烧
（[r/radarr, 2025](https://reddit.com/r/radarr/comments/1km0y9q/)）。官方 wiki
自己把"下载循环"写进了 troubleshooting。

### 主题 G：对私有站/做种不友好

> "the initial pull of downloads to upgrade quality of files has been insane.
> I haven't removed a single torrent from seeding (currently at 78) yet i have
> been banned from two trackers for not getting my seed ratio up quick enough."
> —— [r/trackers, 2023](https://reddit.com/r/trackers/comments/104pwcp/)

> "When Sonarr or Radarr upgrades a release while the old one is still seeding,
> the old file gets removed or replaced… Ideally, upgrades should only happen
> after the required seeding time is completed."
> —— [r/sonarr, 2025](https://reddit.com/r/sonarr/comments/1pneewi/)

hardlink 用户旧种可继续做种，但 copy 模式用户无解；**"做种达标前禁止升级"的
原生开关不存在**，缺口由 Prunerr、qBitrr 等第三方工具填补。洗版对 ratio 经济
是净负担这一点没有任何产品内缓解。

### 主题 H：v4 迁移阵痛

官方 FAQ 承认迁移"tricky"：每行 preferred word 机械转成一个碎片 CF，老用户
升级后要手工清理重建几十上百个 CF；残留的空壳 Release Profiles 页签加剧困惑
（[forums.sonarr.tv, 2023](https://forums.sonarr.tv/t/not-understanding-release-profiles-in-v4/32124)，
无实质官方回复）。教训：**打分体系的 schema 演化没有自动收敛路径**。

---

## 4. 生态与同类产品

### 4.1 TRaSH Guides + 四个同步器 = 策略层外包的代价

- **TRaSH Guides**：社区维护的配置指南，自述"与 Radarr/Sonarr 开发团队密切协作"，
  是事实上的半官方文档。核心资产是 CF 合集（含推荐分数）与四档预设 profile
  （HD Bluray+WEB / UHD Bluray+WEB / Remux+WEB 1080p / 2160p）；
- **Recyclarr**（~2.1k stars）：CLI，把 TRaSH 配置同步进实例，解决三个原生缺陷：
  手工导入成本高、配置会漂移（指南持续更新而原生录入是静态的）、配置存在
  数据库里不可 diff/版本化；
- **Profilarr**（~2.5k stars）：GUI 化的配置管理平台（Build→Test→Deploy），
  配套 Dictionarry 策展数据库；v2 发布帖在 r/selfhosted 获 552 赞；
- **Notifiarr**（付费）/ **Configarr**：同一缺口上的变体。社区愿意为"配置同步"
  付费，佐证缺口的真实性。

**结论：当"正确配置"必须存在于产品之外、由第三方指南承载时，说明产品只交付了
机制（mechanism）而没有交付策略（policy）。** 围绕一个洗版策略长出指南 + 至少
四个同步工具，生态繁荣的另一面是产品把"策略的获取、更新、分发"整层职责甩给了
用户。

来源：[trash-guides.info](https://trash-guides.info/)、
[Guide-Sync](https://trash-guides.info/Guide-Sync/)、
[recyclarr](https://github.com/recyclarr/recyclarr)、
[profilarr](https://github.com/Dictionarry-Hub/profilarr)。

### 4.2 中文方案：MoviePilot / nas-tools

两者共同点：洗版是**订阅上的开关**（而非库的常态行为），质量判定用**规则组
优先级阶梯**（规则按序匹配、首个命中即定级）而非打分叠加，到达最高档位即
**洗版完成、订阅结束**——有明确终点，贴合中文用户"这部洗完了"的心智。

| 维度 | Sonarr/Radarr | MoviePilot / nas-tools |
|---|---|---|
| 质量判定 | 结构化解析 + CF 加法打分（多维叠加） | 规则组首个命中即定级（单维阶梯） |
| 停止条件 | quality cutoff + CF score cutoff 双轨 | 到达规则组最高档位即完成 |
| 洗版归属 | 库的常态行为（长期 monitored） | 订阅生命周期的延长，到顶结束 |
| 文件替换 | 内建（导入即替换 + 回收站） | MoviePilot 依赖"整理覆盖模式"外部配置，配错则洗版不生效 |
| 已有文件评估 | 对现存文件重新打分 | 只记"上次命中的档位"，不感知手工入库文件 |

中文社区反馈要点：

- **好评**：官组优先洗版被称为"毕业级方案"，可用识别词 + 规则组语法实现精细
  控制（[keba 博客](https://hi.keba.host/archives/MOVIEPILOT-Rules)）——但作者
  同时警告需要大量前置配置和手工调校；
- **差评一（操作冗余）**：洗版开关逐订阅勾选，批量订阅重复劳动、易遗漏
  （[CSDN 分析](https://blog.csdn.net/gitblog_07701/article/details/148271162)
  建议全局开关 + 订阅级覆盖）；
- **差评二（粒度不够）**：用户请求"按集洗/完结后统一洗"、独立洗版记录、旧版本
  保留/共存策略，issue 被关闭无实质回应
  （[MoviePilot #2206](https://github.com/jxxghp/MoviePilot/issues/2206)）；
- **差评三（行为不可预期）**：没开洗版开关，1080p 下完后 4K 出现仍被自动下载
  （[MoviePilot #2248](https://github.com/jxxghp/MoviePilot/issues/2248)，
  closed as not planned）——停止条件一旦含糊，用户信任崩塌；
- nas-tools 的洗版逻辑（`over_edition`：新资源优先级**严格大于**已下载记录才抓，
  到顶调 `finish_rss_subscribe` 结束订阅）设计干净，但 wiki 几乎不解释洗版，
  机制靠读源码/社区教程摸索。

### 4.3 简单派的教训：Medusa / SickChill

quality 只有 Allowed（可接受）与 Preferred(理想档) 两组清单，心智负担极低，
但表达力不足的痛点在 issue 里反复出现：无法表达"WEB-DL 优于同分辨率 HDTV 但
劣于蓝光"的偏序（[Medusa #2572](https://github.com/pymedusa/Medusa/issues/2572)）、
无法限制追求 preferred 的时长（[Medusa #3580](https://github.com/pymedusa/Medusa/issues/3580)）。
**两组清单是下限，完整 CF 体系是上限，两端都被用户骂**——说明分层才是出路。

---

## 5. 对 movieclaw 的设计启示

结合 `subscription.md`（决策层预留洗版扩展点、RuleSet 预留 `cutoff_resolution`、
wanted_item 预留质量快照）与本次调研：

### 5.1 必须吸取的六条（按优先级）

1. **停止线只有一条**。Sonarr/Radarr 最大共性痛点是双轨 cutoff 语义分裂
   （#8041、#7634 官方 by design 拒修，用户持续当 bug 报）。movieclaw 的 RuleSet
   已经是"内置评分公式 + 单一评分"的设计，洗版判定应收敛为**单一可解释的序**：
   候选严格优于现状才洗，达到 cutoff（单一定义）即停，不引入第二套并行阈值。
2. **严格大于 + 最小步长**。比较用"严格优于"（nas-tools 同款），并预留最小
   提升步长（arr 系 2024 年才补的 Increment 字段），防同档抖动和微小分差重下。
3. **抓取打分与入库打分必须同源**。arr 系"种子名打分 → 文件名重打分"的不一致
   是至今未根治的死循环根源（#11422）。movieclaw 的优势：`wanted_item` 记录
   grabbed 时的质量快照，洗版比较**永远对着快照比**，不依赖对落地文件的二次
   解析；手工入库文件另行探测（`media_probe` 已有能力）后写入快照。
4. **决策可解释**。"为什么抓/为什么不抓/为什么又抓"是两边社区共同的高频求助。
   movieclaw 已有"拒绝原因落库"的原则，洗版需要同等待遇：每次洗/不洗都落
   结构化原因（质量更低/已达 cutoff/提升不足步长/做种未达标…），详情页可见。
5. **策略内置，不外包**。TRaSH + 四个同步器的生态证明"只给机制不给策略"的
   代价。movieclaw 应内置少数几档预设（如"1080p 即可 / 洗到蓝光 / 洗到 Remux"），
   RuleSet 本来就是"纯参数包 + 内置评分公式不暴露权重"的立场，天然规避了
   "让用户调 50 个 CF 分数"的深坑。正则/自定义规则只留作高级逃生舱。
6. **洗版感知做种状态**。私有站用户的核心诉求"做种达标前不替换"在 arr 系至今
   无原生方案。movieclaw 的 RuleSet 已有 H&R 三态策略和做种下限字段，洗版决策
   应把"旧文件对应种子是否达标/是否 H&R 风险"纳入前置条件——这是对 PT 场景
   最有差异化价值的一条。

### 5.2 可以不背的历史包袱

- **裸正则 CF**：中文 PT 场景的核心洗版维度（分辨率/来源/HDR/DV/官组/中字/
  促销）在 RuleSet 里已是结构化一等公民，不需要让用户写 release title 正则；
- **双轨 cutoff**：一次性设计可直接合并为单一序 + 单一 cutoff；
- **全局布尔开关**（Propers 三态之类）：movieclaw 没有历史存量，不需要。

### 5.3 明确要做的取舍（P6 设计时决策）

- **洗版归属**：MoviePilot/nas-tools 的"订阅内洗版、到顶结束"贴中文心智且状态
  机简单；arr 的"库常态升级"能覆盖手工入库/历史文件。建议第一版取前者
  （洗版是订阅的可选延长阶段），与现有 subscription 状态机自然衔接，
  completed 语义扩展为"wanted 清空且（未开洗版 或 洗版到顶）"；
- **触发通道零新增**：被动匹配（新种子入库尾部直调）天然就是 arr 的 RSS 比对
  模式；主动洗版搜索复用 `next_search_at` 调度与退避，但冷却更长（洗版不急）；
- **文件替换语义内建**：旧文件处理（替换 + 可选保留策略）+ 洗版历史必须是
  功能的一部分，不能像 MoviePilot 一样推给"整理覆盖模式"外部配置；回收站
  机制可复用 `data/` 目录约定；
- **剧集粒度**：沿用 wanted_item 的集级模型（同 Sonarr），整季包洗版遵循
  "对每一集都构成合法升级才接受"的原则（Sonarr `UpgradeDiskSpecification`
  的做法，直接避免整季重洗的浪费）；"完结后统一洗版"（MoviePilot #2206 的
  真实需求）可作为剧集洗版的默认档位。

---

## 6. 来源清单

**官方文档/源码**：
[Radarr Settings](https://wiki.servarr.com/radarr/settings) ·
[Sonarr Settings](https://wiki.servarr.com/sonarr/settings) ·
[Radarr FAQ](https://wiki.servarr.com/radarr/faq) ·
[Sonarr FAQ](https://wiki.servarr.com/sonarr/faq) ·
[Sonarr v4 FAQ](https://wiki.servarr.com/sonarr/faq-v4) ·
[Sonarr Wanted](https://wiki.servarr.com/sonarr/wanted) ·
[UpgradableSpecification.cs](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/UpgradableSpecification.cs) ·
[DownloadDecisionComparer.cs](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/DecisionEngine/DownloadDecisionComparer.cs) ·
[UpgradeDiskSpecification.cs](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/UpgradeDiskSpecification.cs)

**关键 issue**：
[Radarr #4666（CF 优先于质量的诉求，closed）](https://github.com/Radarr/Radarr/issues/4666) ·
[Radarr #8041（CF 升级无视 quality cutoff，by design）](https://github.com/Radarr/Radarr/issues/8041) ·
[Sonarr #5330（关升级不停 CF 升级，已修）](https://github.com/Sonarr/Sonarr/issues/5330) ·
[Sonarr #7634（min score 被高质量绕过，not planned）](https://github.com/Sonarr/Sonarr/issues/7634) ·
[Radarr #9994 / Sonarr #6800（Score Increment 的由来）](https://github.com/Radarr/Radarr/issues/9994) ·
[Radarr #11422（打分不同源导致升级循环，open）](https://github.com/Radarr/Radarr/issues/11422) ·
[Radarr #6924（Cutoff Unmet UI 澄清，open 四年）](https://github.com/Radarr/Radarr/issues/6924)

**生态**：
[TRaSH Guides](https://trash-guides.info/) ·
[TRaSH Quality Profiles](https://trash-guides.info/Radarr/radarr-setup-quality-profiles/) ·
[Recyclarr](https://github.com/recyclarr/recyclarr) ·
[Profilarr](https://github.com/Dictionarry-Hub/profilarr) ·
[Notifiarr TRaSH 集成](https://notifiarr.wiki/pages/integrations/trash/) ·
[Configarr](https://github.com/raydak-labs/configarr)

**中文社区**：
[MoviePilot 订阅 Wiki](https://wiki.movie-pilot.org/subscribe) ·
[MoviePilot #2206](https://github.com/jxxghp/MoviePilot/issues/2206) ·
[MoviePilot #2248](https://github.com/jxxghp/MoviePilot/issues/2248) ·
[官组优先洗版实践（keba）](https://hi.keba.host/archives/MOVIEPILOT-Rules) ·
[nas-tools 过滤规则 Wiki](https://github.com/NAStool/nas-tools-wiki/blob/main/%E8%BF%87%E6%BB%A4%E8%A7%84%E5%88%99.md) ·
nas-tools 源码 `app/subscribe.py` / `app/rss.py`

**同类产品**：
[Medusa Quality Settings](https://github.com/pymedusa/Medusa/wiki/Quality-Settings) ·
[Medusa #2572](https://github.com/pymedusa/Medusa/issues/2572) ·
[SickChill Quality Settings](https://github.com/SickChill/sickchill/wiki/Quality-Settings)

**用户反馈原帖**：正文各引用处已附 URL（Reddit 引文经 Arctic Shift 存档 API
核实原文；Radarr 官方论坛 forums.radarr.video 调研期间不可达，未覆盖）。
