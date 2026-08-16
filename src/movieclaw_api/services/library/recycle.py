"""媒体库文件回收机制：「待回收」第三态的公共延迟删除
（docs/design/library-file-recycle.md）。

与订阅完全解耦的媒体库层公共设施——洗版只是第一个触发方，未来的手动
删除、重复版本清理、整库洗版都复用同一状态、同一展示、同一删除通道。

四个公共函数是状态迁移的唯一写路径（业务代码不得直接改 state）：

- ``recycle_file``：在位 → 待回收。做种保护判据内化在此（唯一硬链接的
  文件绝不改名——它可能正是下载器做种的原始文件）；
- ``restore_file``：待回收 → 在位（恢复）；
- ``purge_file``：立即清理（删物理文件 + 删行）；
- ``purge_due_files``：每小时定时任务，到期清理 + 消失行收敛。

`file_path` 恒为当前物理位置（全站不变量）：移入回收站时 file_path 更新
为回收站内路径，原路径存 ``trash_original_path`` 供恢复。
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.library.layout import SCAN_VIDEO_EXTS, STRM_EXT
from movieclaw_db.engine import get_database
from movieclaw_db.models import FileState, LibraryFile, utcnow
from movieclaw_db.models.library import Library
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.library.recycle")

# 回收站目录名：放在**媒体库根目录内**（与文件同一文件系统，重命名即完成，
# 避免跨盘搬几十 GB）；库扫描跳过点开头目录，不会被重新收编
TRASH_DIR_NAME = ".movieclaw-trash"
DEFAULT_RETENTION = timedelta(days=7)

# 附属文件判定要跳过的扩展名：同名不同容器的视频（含 .iso 原盘镜像、
# .strm 占位）是独立版本不是附属，绝不能被当作字幕/NFO 连带处理。
# 判定规则与 transfer._find_sidecars 同一套约定
_SIDECAR_SKIP_EXTS = SCAN_VIDEO_EXTS | {".iso"}

# purge_after 的哨兵缺省："由机制决定"——移入回收站的按保留期倒计时，
# 做种保护原地待回收的不自动删。触发方明确知道安全时可显式传值覆盖
_AUTO = object()

RecycleOutcome = Literal["moved_to_trash", "kept_in_place", "already_gone"]


def _trash_dir_for(file_path: Path, root_paths: list[str]) -> Path:
    """选文件所属的库根作为回收站落点（前缀匹配）；都不匹配时用文件父目录。"""
    for root in root_paths:
        try:
            file_path.relative_to(root)
        except ValueError:
            continue
        return Path(root) / TRASH_DIR_NAME
    return file_path.parent / TRASH_DIR_NAME


async def _library_roots(session: AsyncSession, library_id: int | None) -> list[str]:
    if library_id is None:
        return []
    row = await session.get(Library, library_id)
    return list(row.root_paths) if row else []


def _sidecar_paths(src: Path) -> list[Path]:
    """主文件的附属文件：同目录、文件名以「主文件名.」开头的字幕/NFO/图片
    （foo.zh.srt / foo.nfo）。视频扩展名是独立版本，排除；原盘目录没有
    附属概念，返回空。判定规则与 transfer._find_sidecars 保持一致。"""
    if src.is_dir() or not src.suffix:
        return []
    try:
        entries = sorted(src.parent.iterdir())
    except OSError:
        return []
    prefix = src.stem + "."
    found: list[Path] = []
    for entry in entries:
        if not entry.is_file() or entry == src or not entry.name.startswith(prefix):
            continue
        if entry.suffix.lower() in _SIDECAR_SKIP_EXTS:
            continue
        found.append(entry)
    return found


def _move_sidecars(src: Path, target: Path) -> None:
    """把 src 的附属文件跟随主文件搬到 target 同目录（target 主名 + 原尾缀，
    如 foo.zh.srt → <target 主名>.zh.srt）。尽力而为：单个失败只记日志，
    不影响主文件已完成的迁移。"""
    for entry in _sidecar_paths(src):
        tail = entry.name[len(src.stem) :]  # 含开头的 "."，如 ".zh.srt"
        dst = target.parent / (target.stem + tail)
        try:
            if dst.exists():
                dst = target.parent / f"{utcnow().strftime('%Y%m%d%H%M%S')}-{dst.name}"
            shutil.move(str(entry), str(dst))
        except OSError:
            logger.warning("附属文件跟随主文件移动失败：%s → %s", entry, dst, exc_info=True)


async def recycle_file(
    session: AsyncSession,
    file: LibraryFile,
    *,
    reason: str,
    trigger: dict[str, Any],
    note: str,
    purge_after: Any = _AUTO,
) -> RecycleOutcome:
    """把在位文件转入待回收（唯一入口）。只改行 + 移文件、不 commit。

    - ``already_gone``：文件已不在磁盘——无物可回收，**调用方负责删行**
      （行留着会与磁盘不一致）；
    - ``moved_to_trash``：已移入库根回收站，默认按保留期倒计时；
    - ``kept_in_place``：做种保护——唯一硬链接（st_nlink ≤ 1）的文件可能
      正是下载器做种的原始文件，改名会打断做种（PT 站 H&R 风险），文件
      原地不动、默认不自动删，只打待回收状态。

    ``trigger``：审计快照 ``{"kind": "subscription"|"member"|"system",
    "id": ..., "label": "《九门》订阅洗版"}``——存快照不外键，机制不认识
    触发方的表。
    """
    now = utcnow()
    src = Path(file.file_path)
    if not src.exists():
        return "already_gone"

    context = {"reason": reason, "trigger": trigger, "note": note}

    def _keep_in_place(why: str) -> RecycleOutcome:
        file.state = FileState.TRASHED
        file.trashed_at = now
        file.trash_original_path = None  # 原地形态：file_path 即原位置
        file.purge_after = None if purge_after is _AUTO else purge_after
        file.trash_context = context
        file.updated_at = now
        logger.info("文件原地待回收（%s）：%s", why, src)
        return "kept_in_place"

    if src.is_dir():
        # 原盘目录（BDMV/VIDEO_TS）：目录的 st_nlink 天然 ≥2，硬链接判据
        # 失效；做种任务引用的是目录路径，改名必断种——一律原地待回收
        return _keep_in_place("原盘目录，改名会打断做种")

    if src.suffix.lower() == STRM_EXT:
        # strm 占位文件是一行播放 URL 的纯文本，不可能是做种载荷；它天然
        # 只有一个硬链接，不豁免会被做种保护永远原地留置、无法自动清理
        seeding_guard = False
    else:
        seeding_guard = True
        try:
            seeding_guard = src.stat().st_nlink <= 1
        except OSError:
            logger.warning("读取文件硬链接数失败，按保守策略原地待回收：%s", src)

    if seeding_guard:
        return _keep_in_place("唯一硬链接，可能仍在做种")

    try:
        trash_dir = _trash_dir_for(src, await _library_roots(session, file.library_id))
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = trash_dir / src.name
        if target.exists():
            target = trash_dir / f"{now.strftime('%Y%m%d%H%M%S')}-{src.name}"
        shutil.move(str(src), str(target))
    except OSError:
        # 移动失败（权限/只读挂载等）：降级为原地待回收、不自动删——行必须
        # 保留（行删了而文件还在，扫描会把它当新文件重新收编），文件去留
        # 交给「恢复/立即清理」或故障修复后的下一轮触发
        logger.exception("移入回收站失败，降级为原地待回收：%s", src)
        return _keep_in_place("移动失败降级")
    # 附属文件（字幕/NFO/图片）跟随主文件进回收站，避免留下无主残留；
    # 恢复时按同一规则搬回
    _move_sidecars(src, target)
    file.trash_original_path = file.file_path
    file.file_path = str(target)
    file.state = FileState.TRASHED
    file.trashed_at = now
    file.purge_after = now + DEFAULT_RETENTION if purge_after is _AUTO else purge_after
    file.trash_context = context
    file.updated_at = now
    logger.info("文件已移入回收站：%s → %s", src, target)
    return "moved_to_trash"


async def restore_file(session: AsyncSession, file: LibraryFile) -> bool:
    """待回收 → 在位（恢复）。原路径被占用时恢复失败、状态原样保留。"""
    if file.state != FileState.TRASHED:
        return False
    now = utcnow()
    if file.trash_original_path:
        src = Path(file.file_path)
        target = Path(file.trash_original_path)
        if target.exists():
            logger.warning("恢复失败：原路径已有同名文件 %s", target)
            return False
        if not src.exists():
            return False  # 文件哪儿都不在——留给清理任务收敛
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        # 跟随进回收站的附属文件（字幕/NFO）按同一规则搬回原目录
        _move_sidecars(src, target)
        file.file_path = str(target)
    elif not Path(file.file_path).exists():
        return False  # 原地形态文件已消失——留给清理任务收敛，不造在位幽灵
    file.state = FileState.IN_PLACE
    file.trashed_at = None
    file.trash_original_path = None
    file.purge_after = None
    file.trash_context = None
    file.updated_at = now
    return True


async def purge_file(session: AsyncSession, file: LibraryFile) -> bool:
    """立即清理：删物理文件 + 删行。做种确认弹窗由 UI 层负责，机制不拦。

    原盘目录形态整树删除前先查台账：监听导入按站点原始目录结构落盘，
    新洗版的文件可能就放在旧原盘目录**里面**——rmtree 会把新版本一起
    炸掉。只要目录下还有其他在案文件行就拒绝清理（爆炸半径保护）。
    """
    if file.state != FileState.TRASHED:
        return False
    path = Path(file.file_path)
    try:
        if path.is_dir():
            if await _has_other_files_under(session, file, path):
                logger.warning(
                    "拒绝清理原盘目录 %s：目录内还有其他在案文件（可能是新入库的版本），"
                    "请先处理目录内文件",
                    path,
                )
                return False
            shutil.rmtree(path)  # 原盘目录形态
        elif path.exists():
            for sidecar in _sidecar_paths(path):
                try:
                    sidecar.unlink()  # 附属文件（字幕/NFO）随主文件一并清理
                except OSError:
                    logger.warning("清理附属文件失败（不影响主文件清理）：%s", sidecar)
            path.unlink()
    except OSError:
        logger.exception("清理待回收文件失败：%s", path)
        return False
    await session.delete(file)
    return True


async def _has_other_files_under(
    session: AsyncSession, file: LibraryFile, directory: Path
) -> bool:
    """目录（前缀）之下是否还有其他台账文件行。LIKE 通配符按字面转义。"""
    prefix = str(directory).rstrip("/") + "/"
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = (
        await session.execute(
            select(LibraryFile.id)
            .where(
                LibraryFile.id != file.id,
                LibraryFile.file_path.like(escaped + "%", escape="\\"),
            )
            .limit(1)
        )
    ).first()
    return row is not None


@register_task(
    "library_recycle_purge",
    title="回收站到期清理",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=3600,
    description="删除超过保留期的待回收文件；待回收期间已消失的行顺带收敛",
)
async def purge_due_files() -> None:
    """每小时：`purge_after <= now` 的删文件+删行；物理已消失的行删行收敛。

    删除滞后至多 1 小时，相对 7 天保留期可忽略；倒计时展示直读
    ``purge_after``，不受执行周期影响；急迫需求由「立即清理」按钮覆盖。
    """
    db = get_database()
    now = utcnow()
    purged = converged = 0
    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.state == FileState.TRASHED)
                )
            ).scalars()
        )
        for file in rows:
            path = Path(file.file_path)
            if not path.exists():
                await session.delete(file)  # 待回收期间文件消失：删行收敛
                converged += 1
                continue
            if (
                file.purge_after is not None
                and file.purge_after <= now
                and await purge_file(session, file)
            ):
                purged += 1
        if purged or converged:
            await session.commit()
            logger.info("回收站到期清理：删除 %d 个文件，收敛 %d 条消失行", purged, converged)


@register_task(
    "library_recycle_orphan_sweep",
    title="回收站孤儿清扫",
    trigger_type=TriggerType.CRON,
    cron="40 4 * * *",
    description="按修改时间清理回收站目录里没有台账行的历史遗留文件",
)
async def sweep_orphan_trash() -> None:
    """每天：清理回收站目录中**没有台账行**的超期文件（机制上线前的旧版本
    回收站文件没有行，只能按 mtime 兜底；有行的文件一律归 purge_due_files
    管，本任务绝不碰）。"""
    db = get_database()
    horizon = utcnow().timestamp() - DEFAULT_RETENTION.total_seconds()
    removed = 0
    async with db.session() as session:
        roots: list[str] = []
        for row in (await session.execute(select(Library))).scalars():
            roots.extend(row.root_paths or [])
        tracked = {
            file.file_path
            for file in (
                await session.execute(
                    select(LibraryFile).where(LibraryFile.state == FileState.TRASHED)
                )
            ).scalars()
        }
    # 在案主文件的附属文件（字幕/NFO 跟随进回收站，mtime 保留原值可能早已
    # 超期）不能当孤儿扫掉——按「同目录 + 主文件名. 前缀」豁免，随主文件
    # 一起走 purge 通道
    tracked_stem_prefixes: dict[str, list[str]] = {}
    for tracked_path in tracked:
        p = Path(tracked_path)
        tracked_stem_prefixes.setdefault(str(p.parent), []).append(p.stem + ".")
    for root in roots:
        trash_dir = Path(root) / TRASH_DIR_NAME
        if not trash_dir.is_dir():
            continue
        stem_prefixes = tracked_stem_prefixes.get(str(trash_dir), [])
        for entry in trash_dir.iterdir():
            if str(entry) in tracked:
                continue
            if any(entry.name.startswith(prefix) for prefix in stem_prefixes):
                continue
            try:
                if entry.stat().st_mtime < horizon:
                    entry.unlink() if entry.is_file() else shutil.rmtree(entry)
                    removed += 1
            except OSError:
                logger.warning("清理回收站孤儿条目失败：%s", entry, exc_info=True)
    if removed:
        logger.info("回收站孤儿清扫：移除 %d 个超期遗留条目", removed)
