"""洗版基线快照：构建、写入与存量回填（docs/design/quality-upgrade.md §4）。

快照取值原则（§4.1）：**能实测的维度以 ffprobe 为准，实测不出的出处维度采信
名称解析**——resolution/hdr/bit_rate 来自 library_file 的 probe 列，
media_source/remux/release_group 优先取投递时的 attempt.quality（种子名解析），
无投递记录（手工入库/扫描收编）时对文件名重跑 enrich。

写入时机：
- 库存对账关闭工单时（wanted_fulfillment 调 ``fill_snapshots``）——所有新
  入库单元统一落快照，与规则组是否开洗版无关（数据热且便宜，规则组随后
  开洗版时立即可用）；
- 存量回填 tick（``backfill_upgrade_snapshots``）——只处理"规则组已配洗版
  目标"的订阅的历史 imported 单元，分批做纯 DB 变换（probe 数据 library_file
  里都有，不重新探测文件）。

快照三态：NULL=未回填；``{}``（空对象）=已尝试构建但关键维度全部无法识别
（不参与洗版，且不会被回填任务反复重试）；非空=正常基线。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    LibraryFile,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_enrich import enrich
from movieclaw_matcher import (
    QualitySnapshot,
    RuleSetSpec,
    build_snapshot,
    resolution_rank,
    source_tier,
)
from movieclaw_scheduler.registry import register_task
from movieclaw_db.models.scheduled_task import TriggerType

logger = logging.getLogger("movieclaw_api.subscription.upgrade")

# 回填每 tick 处理的工单数：回填是低优先级的一次性补账，小批慢跑即可
_BACKFILL_BATCH = 50
_BACKFILL_TICK_SECONDS = 900

# 排期补挂的全表轮转游标（进程内状态；重启丢失只是从头再扫一轮）
_pending_arm_cursor = 0

# 选"最优文件"用的中性偏好（内置默认分辨率序）：快照本身与规则组无关，
# 只有多版本并存时需要一个稳定的挑选顺序
_NEUTRAL_SPEC = RuleSetSpec()


def rule_set_ids_with_upgrade(rule_sets: list[RuleSet]) -> set[int]:
    """解析 spec，返回配置了洗版目标的规则组 id 集合（解析失败视为未配置）。"""
    ids: set[int] = set()
    for row in rule_sets:
        try:
            spec = RuleSetSpec.model_validate(row.spec or {})
        except ValueError:
            continue
        if spec.upgrade_source is not None and row.id is not None:
            ids.add(row.id)
    return ids


def _file_sort_key(file: LibraryFile) -> tuple[int, int, int]:
    """多版本并存时挑最优文件的排序键：分辨率位次 > 片源档 > 新入库优先。"""
    return (
        resolution_rank(file.resolution, _NEUTRAL_SPEC) or 0,
        source_tier(file.media_source, False) or 0,
        file.id or 0,
    )


def snapshot_from_file(
    file: LibraryFile, name_attrs: QualitySnapshot | None
) -> QualitySnapshot:
    """由库文件行 + 名称解析来源构造快照（§4.1 分层取值）。

    ``name_attrs`` 为空时对文件名重跑 enrich（与入库管线同一套解析器与词表），
    并用 library_file 已存的 media_source/release_group 覆盖——它们是入库时
    对**原始名称**的解析结果，比重命名后的文件名更可靠。
    probe 是否成功以"拿到过任一实测值"为据——完全失败时不冒充实测（尤其
    不能把 hdr=None 当成"测得 SDR"覆盖名称信息）。
    """
    if name_attrs is None:
        parsed = enrich(Path(file.file_path).stem)
        name_attrs = QualitySnapshot.model_validate(
            parsed.model_dump(exclude_defaults=True)
        )
        if file.media_source is not None:
            name_attrs.media_source = file.media_source
        if file.release_group is not None:
            name_attrs.release_group = file.release_group
    probed = file.resolution is not None or file.bit_rate is not None
    return build_snapshot(
        name_attrs,
        probed=probed,
        probe_resolution=file.resolution,
        probe_hdr_label=file.hdr,
        probe_bit_rate=file.bit_rate,
    )


async def fill_snapshots(
    session: AsyncSession, media_item_id: int, wanted_rows: list[WantedItem]
) -> None:
    """为一批（同条目的）工单构建并写入质量快照，只改内存行、不 commit。

    找不到在位文件或关键维度全部无法识别时写 ``{}``（已处理哨兵），
    避免回填任务对同一批行无限重试。
    """
    if not wanted_rows:
        return
    files = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    by_unit: dict[tuple[int, int], list[LibraryFile]] = {}
    for file in files:
        by_unit.setdefault((file.season_number, file.episode_number), []).append(file)

    for wanted in wanted_rows:
        unit_files = by_unit.get((wanted.season_number, wanted.episode_number))
        if not unit_files:
            wanted.quality = {}
            continue
        best = max(unit_files, key=_file_sort_key)
        name_attrs: QualitySnapshot | None = None
        # 出处维度优先取投递时的种子名快照（attempt.quality）；快照文件与
        # 投递种子对应关系按"该单元当前 info_hash"定位——手工入库无 attempt
        if wanted.info_hash:
            attempt = (
                await session.execute(
                    select(SubscriptionDownloadAttempt).where(
                        SubscriptionDownloadAttempt.subscription_id
                        == wanted.subscription_id,
                        SubscriptionDownloadAttempt.info_hash == wanted.info_hash,
                    )
                )
            ).scalar_one_or_none()
            if attempt is not None and attempt.quality:
                name_attrs = QualitySnapshot.model_validate(attempt.quality)
        snapshot = snapshot_from_file(best, name_attrs)
        wanted.quality = snapshot.model_dump(exclude_defaults=True)
        wanted.updated_at = utcnow()


# ---------------------------------------------------------------------------
# 洗版 attempt 的工单解析（状态机的关键差异点）
# ---------------------------------------------------------------------------


async def upgrade_attempt_wanted_rows(
    session: AsyncSession,
    attempt: SubscriptionDownloadAttempt,
    *,
    in_scope_only: bool = True,
) -> list[WantedItem]:
    """洗版 attempt 照看的工单：按 attempt.units 定位 **imported** 行。

    缺口语义的关联（``wanted.info_hash == attempt.info_hash ∧ status 在途``）
    对洗版 attempt 恒为空——工单不重开、info_hash 直到确认前仍指向旧版本。
    死种巡检 / 试用目标判定 / 换源候选评估凡是要回答"这个 attempt 还在为谁
    工作"，洗版语义必须走本函数，否则洗版 attempt 会被当成"工单已闭合"
    错误完结（真实教训：投递后首个巡检 tick 即被打成 IMPORTED）。
    """
    units = {
        (int(u[0]), int(u[1]))
        for u in attempt.units
        if isinstance(u, list) and len(u) == 2
    }
    if not units:
        return []
    conditions = [
        WantedItem.subscription_id == attempt.subscription_id,
        WantedItem.status == WantedStatus.IMPORTED,
    ]
    if in_scope_only:
        conditions.append(WantedItem.in_scope.is_(True))  # type: ignore[attr-defined]
    rows = list((await session.execute(select(WantedItem).where(*conditions))).scalars())
    return [r for r in rows if (r.season_number, r.episode_number) in units]


# ---------------------------------------------------------------------------
# 入库验证：实测说了算（quality-upgrade.md §6.3）
# ---------------------------------------------------------------------------

# 回收站目录名：放在**媒体库根目录内**（与文件同一文件系统，重命名即完成，
# 避免跨盘搬 40GB 文件）；保留期满由回填 tick 顺带清理
_TRASH_DIR_NAME = ".movieclaw-trash"
_TRASH_RETENTION_DAYS = 7


def _trash_root_for(file: LibraryFile, root_paths: list[str]) -> Path:
    """选文件所属的库根作为回收站落点（前缀匹配）；都不匹配时用文件父目录。"""
    file_path = Path(file.file_path)
    for root in root_paths:
        try:
            file_path.relative_to(root)
        except ValueError:
            continue
        return Path(root) / _TRASH_DIR_NAME
    return file_path.parent / _TRASH_DIR_NAME


def _move_file_to_trash(file: LibraryFile, root_paths: list[str]) -> tuple[bool, str | None]:
    """把库文件移入回收站；返回 (可安全移除台账行, 回收站内路径)。

    - 源文件已不存在 → (True, None)：行可删（现实里文件早没了）；
    - 移动成功 → (True, 路径)；
    - 移动失败 → **(False, None)**：行必须保留——删了行而文件还在，
      库扫描会把它当新文件重新收编，证伪文件甚至可能借文件名"重生"。
    同名冲突加时间戳前缀。同文件系统内是 rename，瞬时完成。
    """
    import shutil

    src = Path(file.file_path)
    if not src.exists():
        return True, None
    trash_dir = _trash_root_for(file, root_paths)
    try:
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = trash_dir / src.name
        if target.exists():
            target = trash_dir / f"{utcnow().strftime('%Y%m%d%H%M%S')}-{src.name}"
        shutil.move(str(src), str(target))
        return True, str(target)
    except OSError:
        logger.exception("洗版旧版本移入回收站失败：%s", src)
        return False, None


def cleanup_trash_dirs(root_paths: list[str]) -> int:
    """清理回收站中超过保留期的文件，返回清理数（同步，调用方放线程池可选）。"""
    removed = 0
    horizon = utcnow().timestamp() - _TRASH_RETENTION_DAYS * 86400
    for root in root_paths:
        trash_dir = Path(root) / _TRASH_DIR_NAME
        if not trash_dir.is_dir():
            continue
        for entry in trash_dir.iterdir():
            try:
                if entry.stat().st_mtime < horizon:
                    entry.unlink() if entry.is_file() else __import__("shutil").rmtree(entry)
                    removed += 1
            except OSError:
                logger.warning("清理回收站条目失败：%s", entry, exc_info=True)
    return removed


def _file_from_attempt(file: LibraryFile, attempt: SubscriptionDownloadAttempt) -> bool:
    """该库文件是否来自这次洗版投递。

    首选入库来源精确匹配（监听导入会带 site/torrent）；扫描收编的文件没有
    来源信息，退而按时间关联（attempt 创建之后才出现的文件）。
    """
    if file.site_id and attempt.site_id:
        return file.site_id == attempt.site_id and file.torrent_id == attempt.torrent_id
    return file.created_at is not None and attempt.created_at is not None and (
        file.created_at >= attempt.created_at
    )


async def verify_upgrades(session: AsyncSession, media_item_id: int) -> None:
    """洗版入库验证：对该条目在途洗版单元，用实测新快照裁决确认/证伪。

    在库存对账的同一钩子点运行（任何入库路径都会触发），实测说了算：
    - **确认**（新最优文件档位严格高于基线）：刷新快照与 info_hash 关联、
      旧版本文件进回收站、旧任务交给换源清理状态机（CLEANUP_PENDING，
      由 download_progress 巡检以 H&R/所有权/文件重叠证据安全清理）、
      写 UPGRADED 活动并推送；手工塞入的更优文件同样确认（无 attempt 记账）。
    - **证伪**（洗版投递的文件实测不构成升级）：新文件移入回收站、
      attempt 置 FAILED 进排除清单、熔断计数 +1，连续达阈值转入长冷却
      并出 system_notice 提示人工介入。
    """
    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FUSE_COOLDOWN,
        UPGRADE_FUSE_LIMIT,
    )
    from movieclaw_db.models import (
        DownloadAttemptStatus,
        MediaItem,
        SubscriptionActivity,
    )
    from movieclaw_db.models.library import Library
    from movieclaw_db.models.subscription_activity import ActivityType
    from movieclaw_db.repositories import SubscriptionRepository
    from movieclaw_matcher import quality_label

    # 该条目所有"已入库且有基线"的单元
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.media_item_id == media_item_id,
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    rows = [w for w in rows if w.quality]  # 排除 {} 哨兵
    if not rows:
        return
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})

    # 在途洗版 attempt：{(sub_id, unit) -> attempt}
    attempts = list(
        (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id.in_(  # type: ignore[union-attr]
                        {w.subscription_id for w in rows}
                    ),
                    SubscriptionDownloadAttempt.purpose == "upgrade",
                    SubscriptionDownloadAttempt.status.in_(  # type: ignore[attr-defined]
                        (
                            DownloadAttemptStatus.ACTIVE,
                            DownloadAttemptStatus.REPLACEMENT_PENDING,
                            DownloadAttemptStatus.TRIAL,
                            DownloadAttemptStatus.COMPLETED,
                        )
                    ),
                )
            )
        ).scalars()
    )
    attempts_by_unit: dict[tuple[int, tuple[int, int]], list[SubscriptionDownloadAttempt]] = {}
    for attempt in attempts:
        for u in attempt.units:
            if isinstance(u, list) and len(u) == 2:
                attempts_by_unit.setdefault(
                    (attempt.subscription_id, (int(u[0]), int(u[1]))), []
                ).append(attempt)

    files = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id == media_item_id,
                    LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    files_by_unit: dict[tuple[int, int], list[LibraryFile]] = {}
    for file in files:
        files_by_unit.setdefault((file.season_number, file.episode_number), []).append(file)

    # 已证伪 attempt 的来源集合：它们的文件（回收站失败残留 / 意外重复入库）
    # 必须隔离清理，绝不参与最优选择——否则证伪文件会借文件名解析
    # "重生"为一次手工升级
    failed_sources: set[tuple[int, str, str]] = {
        (a.subscription_id, a.site_id, a.torrent_id)
        for a in (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id.in_(  # type: ignore[union-attr]
                        {w.subscription_id for w in rows}
                    ),
                    SubscriptionDownloadAttempt.purpose == "upgrade",
                    SubscriptionDownloadAttempt.status == DownloadAttemptStatus.FAILED,
                )
            )
        ).scalars()
        if a.site_id and a.torrent_id
    }

    from movieclaw_matcher import QualitySnapshot

    item = await session.get(MediaItem, media_item_id)
    repo = SubscriptionRepository(session)
    root_paths_cache: dict[int, list[str]] = {}

    async def _roots(library_id: int | None) -> list[str]:
        if library_id is None:
            return []
        if library_id not in root_paths_cache:
            row = await session.get(Library, library_id)
            root_paths_cache[library_id] = list(row.root_paths) if row else []
        return root_paths_cache[library_id]

    for wanted in rows:
        unit = (wanted.season_number, wanted.episode_number)
        unit_files = files_by_unit.get(unit) or []
        if len(unit_files) < 2 and (wanted.subscription_id, unit) not in attempts_by_unit:
            continue  # 单版本且无在途洗版：没有可裁决的事
        quarantine = [
            f
            for f in unit_files
            if f.site_id
            and (wanted.subscription_id, f.site_id, f.torrent_id) in failed_sources
        ]
        if quarantine and len(quarantine) < len(unit_files):
            for file in quarantine:
                removable, _trashed = _move_file_to_trash(
                    file, await _roots(file.library_id)
                )
                if removable:
                    await session.delete(file)
            await session.commit()
            unit_files = [f for f in unit_files if f not in quarantine]
        baseline = QualitySnapshot.model_validate(wanted.quality)
        spec = specs.get(wanted.subscription_id)
        # 同一单元可能同时挂着旧洗版源（等替换）与试用源两个 attempt：优先
        # 选与单元文件来源精确对应的那个（site/torrent 匹配），避免把试用源
        # 交付的文件记到死源头上；无精确匹配时取最新创建的
        unit_attempts = attempts_by_unit.get((wanted.subscription_id, unit), [])
        attempt = None
        for att in sorted(
            unit_attempts, key=lambda a: (a.created_at or utcnow(), a.id or 0), reverse=True
        ):
            if att.site_id and any(
                f.site_id == att.site_id and f.torrent_id == att.torrent_id
                for f in files_by_unit.get(unit, [])
            ):
                attempt = att
                break
        if attempt is None and unit_attempts:
            attempt = max(
                unit_attempts, key=lambda a: (a.created_at or utcnow(), a.id or 0)
            )
        # 混合投递（缺口+洗版同一 attempt）里，缺口单元也在 attempt.units 中，
        # 但它是靠这次投递才入库的——不是洗版目标，绝不能拿它走证伪分支。
        # 判据：单元必须在 attempt 创建之前就已入库，才算该 attempt 要洗的对象
        if (
            attempt is not None
            and (
                wanted.imported_at is None
                or attempt.created_at is None
                or wanted.imported_at > attempt.created_at
            )
        ):
            attempt = None

        # 逐文件算快照，按**快照**找最优（名称来源：来自洗版投递的文件用
        # attempt.quality——文件行本身可能还没有片源信息）
        from movieclaw_matcher.decision import resolution_rank, source_tier

        best_file: LibraryFile | None = None
        best_snapshot: QualitySnapshot | None = None
        best_key: tuple[int, int, int] = (-1, -1, -1)
        snapshots_by_file: dict[int, QualitySnapshot] = {}
        for file in unit_files:
            name_attrs = None
            if attempt is not None and attempt.quality and _file_from_attempt(file, attempt):
                name_attrs = QualitySnapshot.model_validate(attempt.quality)
            snapshot = snapshot_from_file(file, name_attrs)
            if file.id is not None:
                snapshots_by_file[file.id] = snapshot
            key = (
                resolution_rank(snapshot.resolution, _NEUTRAL_SPEC) or 0,
                source_tier(snapshot.media_source, snapshot.remux) or 0,
                file.id or 0,
            )
            if key > best_key:
                best_file, best_snapshot, best_key = file, snapshot, key

        if best_file is None or best_snapshot is None or spec is None:
            continue

        # 验证是不设停止线的纯序比较：实测新快照严格优于基线即确认——
        # 手工塞入超过洗版目标的版本同样是合法升级（quality-upgrade.md §6.3）
        upgrade_enabled = spec.upgrade_source is not None
        if _better(best_snapshot, baseline, spec):
            if not upgrade_enabled:
                # 未开洗版：只静默把基线刷新为实测最优（保持台账真实，日后
                # 开洗版立即可用），**绝不**替换/移动用户的文件——删除性动作
                # 必须有洗版目标这个显式 opt-in
                wanted.quality = best_snapshot.model_dump(exclude_defaults=True)
                wanted.updated_at = utcnow()
                await session.commit()
                continue
            # ---- 确认升级 ----
            now = utcnow()
            old_hash = wanted.info_hash
            new_label = quality_label(best_snapshot)
            old_label = quality_label(baseline)
            wanted.quality = best_snapshot.model_dump(exclude_defaults=True)
            wanted.upgrade_verify_failures = 0
            wanted.updated_at = now
            trash_paths: list[str] = []
            if attempt is not None and _file_from_attempt(best_file, attempt):
                wanted.info_hash = attempt.info_hash
                attempt.status = DownloadAttemptStatus.IMPORTED
                attempt.cleanup_note = "洗版完成：新版本已入库"
                attempt.updated_at = now
                # 旧任务交给换源清理状态机（H&R/所有权/文件重叠证据齐备才删）
                if old_hash and old_hash != attempt.info_hash:
                    old_attempt = (
                        await session.execute(
                            select(SubscriptionDownloadAttempt).where(
                                SubscriptionDownloadAttempt.subscription_id
                                == wanted.subscription_id,
                                SubscriptionDownloadAttempt.info_hash == old_hash,
                            )
                        )
                    ).scalar_one_or_none()
                    if old_attempt is not None and old_attempt.status not in (
                        DownloadAttemptStatus.SUPERSEDED,
                        DownloadAttemptStatus.RETAINED,
                        DownloadAttemptStatus.CANCELLED,
                    ):
                        if attempt.replaces_attempt_id in (None, old_attempt.id):
                            attempt.replaces_attempt_id = old_attempt.id
                            old_attempt.status = DownloadAttemptStatus.CLEANUP_PENDING
                        else:
                            # 整季包一次替换多个来源不同的旧单集时，replaces
                            # 指针只能指向一个旧 attempt——清理巡检靠这个指针
                            # 找"新源"读取证据，指不到的旧 attempt 挂进
                            # CLEANUP_PENDING 只会永远等不到清理。其余旧任务
                            # 保守保留做种（与"证据不足不删数据"的换源铁律一致）
                            old_attempt.status = DownloadAttemptStatus.RETAINED
                            old_attempt.cleanup_note = (
                                "洗版整季替换：多个旧任务无法自动比对文件重叠，"
                                "保留做种，可在任务中心手动清理"
                            )
                        old_attempt.updated_at = now
            # 旧版本文件进回收站、台账行移除（quality-upgrade.md §7.1）
            for file in unit_files:
                if file.id == best_file.id:
                    continue
                removable, trashed = _move_file_to_trash(file, await _roots(file.library_id))
                if trashed:
                    trash_paths.append(trashed)
                if removable:
                    await session.delete(file)
                # 移动失败时保留台账行：行删了而文件还在，扫描会把它当新
                # 文件重新收编（台账必须与磁盘一致）
            await session.commit()
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=wanted.subscription_id,
                    wanted_item_id=wanted.id,
                    type=ActivityType.UPGRADED,
                    message=(
                        f"{_unit_text(wanted)}已洗版：{old_label} → {new_label}"
                        + ("，旧版本已移入回收站（保留 7 天）" if trash_paths else "")
                    ),
                    payload={
                        "from": old_label,
                        "to": new_label,
                        "trash_paths": trash_paths,
                        "units": [[wanted.season_number, wanted.episode_number]],
                    },
                )
            )
            if item is not None:
                from movieclaw_api.services.channel_push import (
                    notify_channels,
                    tmdb_push_image_url,
                )

                notify_channels(
                    f"✨ 已洗版:《{item.title}》{_unit_text(wanted)}\n{old_label} → {new_label}",
                    event="upgraded",
                    image_url=tmdb_push_image_url(item.backdrop_path, item.poster_path),
                )
            logger.info(
                "洗版完成：条目 #%s %s %s → %s",
                media_item_id,
                _unit_text(wanted),
                old_label,
                new_label,
            )
        elif attempt is not None and any(
            _file_from_attempt(f, attempt) for f in unit_files
        ):
            # ---- 洗版投递的文件已入库但不构成升级：区分"造假"与"被抢先" ----
            now = utcnow()
            trash_paths = []
            from_attempt = [f for f in unit_files if _file_from_attempt(f, attempt)]
            others = [f for f in unit_files if not _file_from_attempt(f, attempt)]
            # 防御：旧版本文件必须还在位才移走证伪文件（宁可留下劣质版本，
            # 也绝不把单元清空）
            if others:
                for file in from_attempt:
                    removable, trashed = _move_file_to_trash(
                        file, await _roots(file.library_id)
                    )
                    if trashed:
                        trash_paths.append(trashed)
                    if removable:
                        await session.delete(file)
            # 资源是否诚实：投递文件的实测快照没有低于其声称档位 → 资源没
            # 撒谎，只是基线在下载期间被更优版本（如手工入库）抢先刷高。
            # 诚实资源不计熔断、不进排除清单、不写证伪活动——错误的惩罚会
            # 把好资源永久拉黑
            claimed = QualitySnapshot.model_validate(attempt.quality or {})
            attempt_measured = [
                snapshots_by_file[f.id] for f in from_attempt if f.id in snapshots_by_file
            ]
            honest = any(not _better(claimed, m, spec) for m in attempt_measured)
            if honest:
                attempt.status = DownloadAttemptStatus.CANCELLED
                attempt.cleanup_note = (
                    "洗版下载完成时基线已被更优版本抢先，本次结果不再需要；"
                    "保留下载器任务"
                )
                attempt.updated_at = now
                await session.commit()
                logger.info(
                    "洗版抢先收口：条目 #%s %s 的洗版结果已被更优版本取代",
                    media_item_id,
                    _unit_text(wanted),
                )
                continue
            attempt.status = DownloadAttemptStatus.FAILED
            attempt.cleanup_note = "洗版证伪：实测档位不高于当前版本，候选已排除"
            attempt.updated_at = now
            wanted.upgrade_verify_failures += 1
            fused = wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT
            if fused:
                wanted.next_search_at = now + UPGRADE_FUSE_COOLDOWN
            wanted.updated_at = now
            await session.commit()
            await repo.add_activity(
                SubscriptionActivity(
                    subscription_id=wanted.subscription_id,
                    wanted_item_id=wanted.id,
                    type=ActivityType.UPGRADE_VERIFY_FAILED,
                    message=(
                        f"{_unit_text(wanted)}洗版候选证伪：标称 "
                        f"{quality_label(QualitySnapshot.model_validate(attempt.quality or {}))}，"
                        f"实测为 {quality_label(best_snapshot)}，已排除该资源"
                        + (
                            f"；连续 {wanted.upgrade_verify_failures} 次证伪，"
                            "该单元洗版转入 30 天冷却"
                            if fused
                            else ""
                        )
                    ),
                    payload={
                        "site_id": attempt.site_id,
                        "torrent_id": attempt.torrent_id,
                        "claimed": attempt.quality,
                        "measured": best_snapshot.model_dump(exclude_defaults=True),
                        "verify_failures": wanted.upgrade_verify_failures,
                    },
                )
            )
            if fused:
                from movieclaw_db.models.system_notice import NoticeSeverity

                from movieclaw_api.services.system_notice import upsert_notice

                await upsert_notice(
                    session,
                    dedupe_key=(
                        f"subscription.upgrade:{wanted.subscription_id}:"
                        f"{wanted.season_number}:{wanted.episode_number}"
                    ),
                    severity=NoticeSeverity.WARNING,
                    source="subscription",
                    title="洗版连续证伪，已暂停该单元",
                    message=(
                        f"《{item.title if item else '未知条目'}》{_unit_text(wanted)}"
                        f"连续 {wanted.upgrade_verify_failures} 次抓到标称与实测不符的资源，"
                        "洗版已转入 30 天冷却。可在订阅详情检查候选质量或调整规则组。"
                    ),
                )
            logger.warning(
                "洗版证伪：条目 #%s %s 标称与实测不符（连续 %d 次）",
                media_item_id,
                _unit_text(wanted),
                wanted.upgrade_verify_failures,
            )


def _better(snapshot, baseline, spec) -> bool:
    """实测快照是否严格优于基线（不设停止线的纯序比较，供确认路径复用）。"""
    from movieclaw_matcher.decision import resolution_rank, source_tier

    s_res = resolution_rank(snapshot.resolution, spec)
    b_res = resolution_rank(baseline.resolution, spec)
    if s_res is None or b_res is None:
        return False
    if s_res != b_res:
        return s_res > b_res
    s_tier = source_tier(snapshot.media_source, snapshot.remux)
    b_tier = source_tier(baseline.media_source, baseline.remux)
    return s_tier is not None and b_tier is not None and s_tier > b_tier


def _unit_text(wanted: WantedItem) -> str:
    if wanted.season_number == 0 and wanted.episode_number == 0:
        return "正片"
    return f"S{wanted.season_number:02d}E{wanted.episode_number:02d}"


# ---------------------------------------------------------------------------
# 洗版搜索调度（quality-upgrade.md §6.4：被动为主，主动极低频）
# ---------------------------------------------------------------------------


async def _specs_for_subscriptions(
    session: AsyncSession, subscription_ids: set[int]
) -> dict[int, RuleSetSpec | None]:
    """{subscription_id: 解析后的规则组 spec}；解析失败为 None（跳过洗版）。"""
    if not subscription_ids:
        return {}
    subs = list(
        (
            await session.execute(
                select(Subscription).where(Subscription.id.in_(subscription_ids))  # type: ignore[union-attr]
            )
        ).scalars()
    )
    rule_ids = {s.rule_set_id for s in subs}
    rules = {
        r.id: r
        for r in (
            await session.execute(select(RuleSet).where(RuleSet.id.in_(rule_ids)))  # type: ignore[union-attr]
        ).scalars()
    }
    result: dict[int, RuleSetSpec | None] = {}
    for sub in subs:
        rule = rules.get(sub.rule_set_id)
        try:
            result[sub.id] = RuleSetSpec.model_validate(rule.spec or {} if rule else {})
        except ValueError:
            result[sub.id] = None
    return result


async def arm_upgrade_candidates(session: AsyncSession, wanted_rows: list[WantedItem]) -> int:
    """给可洗版的 imported 单元排洗版搜索（首搜在 24h 内错峰）。

    只改内存行、不 commit（跟随调用方事务）。触发点：入库对账落快照后、
    存量回填后。被动匹配不依赖排期（上下文实时读 spec），排期只服务
    主动搜索兜底。返回排期数。
    """
    import random

    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FIRST_SEARCH_SPREAD_HOURS,
        UPGRADE_PRIORITY,
        upgrade_ready,
    )

    rows = [w for w in wanted_rows if w.status == WantedStatus.IMPORTED and w.in_scope]
    if not rows:
        return 0
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})
    now = utcnow()
    armed = 0
    for wanted in rows:
        spec = specs.get(wanted.subscription_id)
        if spec is None or spec.upgrade_source is None:
            continue
        if not upgrade_ready(wanted, spec, now=now):
            continue
        wanted.priority = UPGRADE_PRIORITY
        wanted.next_search_at = now + timedelta(
            seconds=random.uniform(0, UPGRADE_FIRST_SEARCH_SPREAD_HOURS * 3600)
        )
        wanted.search_attempts = 0  # 调度字段进入洗版语义，退避曲线重新起步
        wanted.updated_at = now
        armed += 1
    return armed


async def reset_upgrade_search_now(session: AsyncSession, subscription_id: int) -> int:
    """「立即搜索」的洗版半边：把该订阅可洗的单元全部重置为立刻到期。

    只碰"当下确实可洗"的单元（到顶/不可比/熔断冷却中的不碰）。
    只改内存行、不 commit（跟随调用方事务）。返回重置数。
    """
    from movieclaw_api.services.subscription.matching import (
        UPGRADE_FUSE_LIMIT,
        UPGRADE_PRIORITY,
    )
    from movieclaw_matcher import provably_below_cutoff

    specs = await _specs_for_subscriptions(session, {subscription_id})
    spec = specs.get(subscription_id)
    if spec is None or spec.upgrade_source is None:
        return 0
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.subscription_id == subscription_id,
                    WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                )
            )
        ).scalars()
    )
    now = utcnow()
    reset = 0
    for wanted in rows:
        if not wanted.quality:
            continue
        if not provably_below_cutoff(QualitySnapshot.model_validate(wanted.quality), spec):
            continue
        if wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT:
            # 「立即搜索」正是熔断提示（system_notice）要求的人工介入：
            # 解除熔断、清零计数重新观察，并熄灭对应提示
            from movieclaw_api.services.system_notice import resolve_notices

            wanted.upgrade_verify_failures = 0
            await resolve_notices(
                session,
                prefix=(
                    f"subscription.upgrade:{subscription_id}:"
                    f"{wanted.season_number}:{wanted.episode_number}"
                ),
            )
        wanted.priority = UPGRADE_PRIORITY
        wanted.next_search_at = now
        wanted.updated_at = now
        reset += 1
    return reset


async def postpone_upgrade_wanted(
    session: AsyncSession, media_id: int, *, delay: timedelta | None, count_attempt: bool
) -> int:
    """给该条目下到期未洗成的洗版单元排下一次搜索（worker 退避记账的洗版半边）。

    自愈：单元已不可洗（到顶/规则组撤销洗版/熔断冷却未到）→ 解除排期
    （next_search_at=None），不再打扰站点。返回顺延数。
    """
    from movieclaw_api.services.subscription.matching import (
        upgrade_backoff_delay,
        upgrade_ready,
    )

    now = utcnow()
    rows = list(
        (
            await session.execute(
                select(WantedItem).where(
                    WantedItem.media_item_id == media_id,
                    WantedItem.status == WantedStatus.IMPORTED,
                    WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                    WantedItem.next_search_at.isnot(None),  # type: ignore[union-attr]
                    WantedItem.next_search_at <= now,  # type: ignore[operator]
                )
            )
        ).scalars()
    )
    if not rows:
        return 0
    specs = await _specs_for_subscriptions(session, {w.subscription_id for w in rows})
    from movieclaw_api.services.subscription.matching import UPGRADE_FUSE_LIMIT

    postponed = 0
    for wanted in rows:
        spec = specs.get(wanted.subscription_id)
        if spec is None or spec.upgrade_source is None or not upgrade_ready(
            wanted, spec, now=now
        ):
            wanted.next_search_at = None  # 自愈解除排期
            wanted.updated_at = now
            continue
        # 熔断冷却已到期的单元走到这里说明它重新参赛：计数清零重新观察——
        # 否则常规 7d 退避会被 upgrade_ready 误判成"仍在冷却"，把被动匹配
        # （洗版主通道）无限期关掉
        if wanted.upgrade_verify_failures >= UPGRADE_FUSE_LIMIT:
            wanted.upgrade_verify_failures = 0
        if count_attempt:
            wanted.next_search_at = now + upgrade_backoff_delay(wanted.search_attempts)
            wanted.search_attempts += 1
            wanted.last_search_at = now
        else:
            wanted.next_search_at = now + (delay or timedelta(minutes=15))
        wanted.updated_at = now
        postponed += 1
    await session.commit()
    return postponed


@register_task(
    "backfill_upgrade_snapshots",
    title="洗版基线回填",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=_BACKFILL_TICK_SECONDS,
    description=(
        "为已配置洗版目标的规则组所引用订阅，补齐历史已入库单元的质量快照"
        "（洗版比较的基线）。纯数据库变换、分批慢跑，补完即空转。"
    ),
)
async def backfill_upgrade_snapshots() -> None:
    """存量回填 tick：每次最多处理一批 quality IS NULL 的 imported 单元。"""
    db = get_database()
    async with db.session() as session:
        rule_sets = list((await session.execute(select(RuleSet))).scalars().all())
        upgrade_ids = rule_set_ids_with_upgrade(rule_sets)
        if not upgrade_ids:
            return
        rows = list(
            (
                await session.execute(
                    select(WantedItem)
                    .join(
                        Subscription, Subscription.id == WantedItem.subscription_id
                    )
                    .where(
                        WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                        WantedItem.quality.is_(None),  # type: ignore[union-attr]
                        Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                    )
                    .limit(_BACKFILL_BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        by_media: dict[int, list[WantedItem]] = {}
        for row in rows:
            by_media.setdefault(row.media_item_id, []).append(row)
        for media_item_id, wanted_rows in by_media.items():
            await fill_snapshots(session, media_item_id, wanted_rows)
        armed = await arm_upgrade_candidates(session, rows)
        await session.commit()
        logger.info(
            "洗版基线回填：本轮补齐 %d 个单元的质量快照，%d 个进入洗版排期",
            len(rows),
            armed,
        )

        # 已有快照但尚未排期的单元（如规则组事后才配洗版目标）：本 tick 顺带
        # 补排期。查询条件表达不了"可洗"（到顶/{} 哨兵会被 arm 判否留在
        # NULL），所以用 id 游标轮转全表——既不让同一批判否行每 tick 重扫，
        # 也不让 LIMIT 把排在后面的真正可洗单元饿死
        global _pending_arm_cursor
        pending = list(
            (
                await session.execute(
                    select(WantedItem)
                    .join(Subscription, Subscription.id == WantedItem.subscription_id)
                    .where(
                        WantedItem.id > _pending_arm_cursor,  # type: ignore[operator]
                        WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                        WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                        WantedItem.next_search_at.is_(None),  # type: ignore[union-attr]
                        Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                    )
                    .order_by(WantedItem.id)  # type: ignore[arg-type]
                    .limit(_BACKFILL_BATCH * 4)
                )
            ).scalars()
        )
        # 游标是进程内状态：重启丢失只意味着从头再扫一轮，无害
        _pending_arm_cursor = (pending[-1].id or 0) if pending else 0
        if pending:
            armed_late = await arm_upgrade_candidates(session, pending)
            if armed_late:
                await session.commit()
                logger.info("洗版排期补挂：%d 个已有快照的单元进入洗版排期", armed_late)

        # 顺带清理各库回收站中超保留期的旧版本文件
        from movieclaw_db.models.library import Library

        roots: list[str] = []
        for row in (await session.execute(select(Library))).scalars():
            roots.extend(row.root_paths or [])
        if roots:
            removed = cleanup_trash_dirs(roots)
            if removed:
                logger.info("回收站清理：移除 %d 个超过 %d 天的旧版本文件", removed, _TRASH_RETENTION_DAYS)
