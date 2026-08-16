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
    from movieclaw_api.services.subscription.matching import UPGRADE_PRIORITY, upgrade_ready

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
        if not upgrade_ready(wanted, spec, now=now):
            continue
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
    postponed = 0
    for wanted in rows:
        spec = specs.get(wanted.subscription_id)
        if spec is None or spec.upgrade_source is None or not upgrade_ready(
            wanted, spec, now=now
        ):
            wanted.next_search_at = None  # 自愈解除排期
            wanted.updated_at = now
            continue
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
        # 补排期。到顶/不可比的单元会被 arm 判否留在 NULL——查询量小，可接受
        pending = list(
            (
                await session.execute(
                    select(WantedItem)
                    .join(Subscription, Subscription.id == WantedItem.subscription_id)
                    .where(
                        WantedItem.status == WantedStatus.IMPORTED,  # type: ignore[arg-type]
                        WantedItem.in_scope.is_(True),  # type: ignore[attr-defined]
                        WantedItem.quality.isnot(None),  # type: ignore[union-attr]
                        WantedItem.next_search_at.is_(None),  # type: ignore[union-attr]
                        Subscription.rule_set_id.in_(upgrade_ids),  # type: ignore[union-attr]
                    )
                    .limit(_BACKFILL_BATCH * 4)
                )
            ).scalars()
        )
        if pending:
            armed_late = await arm_upgrade_candidates(session, pending)
            if armed_late:
                await session.commit()
                logger.info("洗版排期补挂：%d 个已有快照的单元进入洗版排期", armed_late)
