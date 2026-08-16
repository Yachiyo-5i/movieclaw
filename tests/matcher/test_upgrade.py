"""洗版判定测试（docs/design/quality-upgrade.md §2/§4/§5）。

覆盖：档位阶梯字典序、停止线、部分可比（未知维度）、分辨率虚标场景下的
快照构造（实测优先）、目标兜底缺省、spec 校验。
"""

from __future__ import annotations

import pytest
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import (
    QualitySnapshot,
    RuleSetSpec,
    TorrentCandidate,
    build_snapshot,
    compare_upgrade,
    provably_below_cutoff,
    quality_label,
    upgrade_target_label,
)


def _candidate(**attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test", torrent_id="1", title="t", subtitle="", attrs=TorrentAttrs(**attrs)
    )


def _spec(**kwargs) -> RuleSetSpec:
    return RuleSetSpec(**kwargs)


def _snap(**kwargs) -> QualitySnapshot:
    return QualitySnapshot(**kwargs)


# ---------------------------------------------------------------------------
# 档位比较：分辨率严格优先，同分辨率比片源档
# ---------------------------------------------------------------------------


UPGRADE_CASES = [
    # (当前快照, 候选 attrs, spec kwargs, 期望 accepted, 期望 reason_code)
    # 片源档升级：WEB-DL → Remux
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        True,
        None,
    ),
    # 片源档升级：WEBRip → WEB-DL
    (
        dict(resolution="1080p", media_source="WEBRip"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        True,
        None,
    ),
    # 同档不洗（离散档位免疫抖动）：都是 WEB-DL
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 片源降档拒绝：蓝光 → WEB-DL
    (
        dict(resolution="1080p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 分辨率升级不看片源：720p 蓝光 → 1080p WEBRip 也算升级（分辨率严格优先）
    (
        dict(resolution="720p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="WEBRip"),
        dict(upgrade_source="remux", cutoff_resolution=None),
        True,
        None,
    ),
    # 分辨率降档拒绝
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="720p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_better",
    ),
    # 停止线：已达目标（1080p Remux 达到 remux 目标，默认目标分辨率 1080p）
    (
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(resolution="1080p", media_source="UHD Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_at_cutoff",
    ),
    # 停止线：blu-ray 目标下蓝光重编码即到顶，Remux 不再洗
    (
        dict(resolution="1080p", media_source="Blu-ray"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="blu-ray"),
        False,
        "upgrade_at_cutoff",
    ),
    # web-dl 目标：WEBRip → WEB-DL 仍可洗
    (
        dict(resolution="1080p", media_source="WEBRip"),
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(upgrade_source="web-dl"),
        True,
        None,
    ),
    # 候选片源未知：同分辨率下无法证明升级
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="1080p"),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 当前片源未知：同分辨率判否，但分辨率升级仍可证明（见下一条）
    (
        dict(resolution="1080p"),
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 当前片源未知 + 候选分辨率更高：分辨率维度可证明，接受
    (
        dict(resolution="1080p"),
        dict(resolution="2160p", media_source="WEB-DL"),
        dict(upgrade_source="remux", cutoff_resolution="2160p", resolutions=["2160p", "1080p"]),
        True,
        None,
    ),
    # 候选分辨率未知：不可比
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
        "upgrade_not_comparable",
    ),
    # 用户偏好 1080p 优先于 2160p（省空间党）：2160p 候选按偏好序判"更低"
    (
        dict(resolution="1080p", media_source="WEB-DL"),
        dict(resolution="2160p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux", resolutions=["1080p", "2160p"]),
        False,
        "upgrade_not_better",
    ),
]


@pytest.mark.parametrize("snap_kw, cand_kw, spec_kw, accepted, code", UPGRADE_CASES)
def test_compare_upgrade_table(snap_kw, cand_kw, spec_kw, accepted, code) -> None:
    verdict = compare_upgrade(_candidate(**cand_kw), _snap(**snap_kw), _spec(**spec_kw))
    assert verdict.accepted is accepted
    assert verdict.reason_code == code
    if not accepted:
        assert verdict.reason_text  # 拒绝必须带完整中文句


def test_compare_upgrade_requires_enabled_spec() -> None:
    """未配置洗版目标时调用是编排层 bug，直接抛错而非静默判否。"""
    with pytest.raises(ValueError):
        compare_upgrade(
            _candidate(resolution="1080p"), _snap(resolution="720p"), _spec()
        )


def test_accepted_verdict_carries_labels() -> None:
    """接受时带 from/to 档位标签，供"从 X 洗到 Y"活动文案。"""
    verdict = compare_upgrade(
        _candidate(resolution="1080p", media_source="Blu-ray", remux=True),
        _snap(resolution="1080p", media_source="WEB-DL"),
        _spec(upgrade_source="remux"),
    )
    assert verdict.accepted
    assert verdict.current_label == "1080p WEB-DL"
    assert verdict.candidate_label == "1080p Remux"


# ---------------------------------------------------------------------------
# 调度口径：只有可证明低于目标的单元参与洗版
# ---------------------------------------------------------------------------


BELOW_CUTOFF_CASES = [
    # (快照, spec kwargs, 期望)
    # WEB-DL 低于 remux 目标 → 参与
    (dict(resolution="1080p", media_source="WEB-DL"), dict(upgrade_source="remux"), True),
    # 已是 Remux → 到顶
    (
        dict(resolution="1080p", media_source="Blu-ray", remux=True),
        dict(upgrade_source="remux"),
        False,
    ),
    # 分辨率低于目标：片源即使未知也可证明"还差着"
    (
        dict(resolution="720p"),
        dict(upgrade_source="remux", resolutions=["1080p", "720p"], cutoff_resolution="1080p"),
        True,
    ),
    # 同分辨率片源未知 → 证明不了差距，安静
    (dict(resolution="1080p"), dict(upgrade_source="remux"), False),
    # 快照分辨率未知 → 不参与
    (dict(media_source="WEB-DL"), dict(upgrade_source="remux"), False),
    # 分辨率不在偏好序（1080p-only 规则下的手工 480p 文件）→ 位次未知，安静
    (
        dict(resolution="480p", media_source="WEB-DL"),
        dict(upgrade_source="remux", resolutions=["1080p"]),
        False,
    ),
    # 分辨率已超目标（2160p 文件、目标 1080p）→ 到顶
    (
        dict(resolution="2160p", media_source="WEB-DL"),
        dict(
            upgrade_source="remux",
            resolutions=["2160p", "1080p"],
            cutoff_resolution="1080p",
        ),
        False,
    ),
    # 未配置洗版 → 永不参与
    (dict(resolution="720p", media_source="HDTV"), dict(), False),
]


@pytest.mark.parametrize("snap_kw, spec_kw, expected", BELOW_CUTOFF_CASES)
def test_provably_below_cutoff(snap_kw, spec_kw, expected) -> None:
    assert provably_below_cutoff(_snap(**snap_kw), _spec(**spec_kw)) is expected


def test_below_cutoff_none_snapshot() -> None:
    assert provably_below_cutoff(None, _spec(upgrade_source="remux")) is False


# ---------------------------------------------------------------------------
# 快照构造：实测优先，出处采信名称（§4.1）
# ---------------------------------------------------------------------------


def test_snapshot_probe_overrides_name_resolution() -> None:
    """分辨率虚标场景：种子标称 2160p，实测 1080p——实测说了算。"""
    name = TorrentAttrs(resolution="2160p", media_source="WEB-DL", release_group="FAKE")
    snap = build_snapshot(
        name, probed=True, probe_resolution="1080p", probe_hdr_label=None, probe_bit_rate=8_000_000
    )
    assert snap.resolution == "1080p"
    assert snap.media_source == "WEB-DL"  # 出处采信名称
    assert snap.release_group == "FAKE"
    assert snap.hdr == []  # probe 测得 SDR 是已知空，不是未知
    assert snap.bit_rate == 8_000_000


def test_snapshot_probe_hdr_normalized_to_vocab() -> None:
    """probe 的 "Dolby Vision" 归一为词表值 "DV"，两侧命名空间在此消化。"""
    name = TorrentAttrs(resolution="2160p", media_source="Blu-ray", remux=True, hdr=["HDR10"])
    snap = build_snapshot(name, probed=True, probe_resolution="2160p", probe_hdr_label="Dolby Vision")
    assert snap.hdr == ["DV"]
    assert snap.remux is True


def test_snapshot_probe_resolution_missing_falls_back_to_name() -> None:
    """探测失败拿不到分辨率时回落名称值（名称是仅剩的信息）。"""
    name = TorrentAttrs(resolution="1080p", media_source="WEB-DL")
    snap = build_snapshot(name, probed=True, probe_resolution=None)
    assert snap.resolution == "1080p"


def test_snapshot_name_only_path() -> None:
    """纯名称路径（无 probe）：全部取名称解析值。"""
    name = TorrentAttrs(resolution="1080p", media_source="WEB-DL", hdr=["HDR10"])
    snap = build_snapshot(name)
    assert snap.resolution == "1080p" and snap.hdr == ["HDR10"]


def test_snapshot_from_attempt_quality_dict() -> None:
    """attempt.quality（TorrentAttrs 的 exclude_defaults dump）可直接
    validate 成快照——多余字段被忽略，同名字段值域一致。"""
    dumped = TorrentAttrs(
        resolution="1080p", media_source="WEB-DL", seasons=[1], episodes=[2]
    ).model_dump(exclude_defaults=True)
    snap = QualitySnapshot.model_validate(dumped)
    assert snap.resolution == "1080p" and snap.media_source == "WEB-DL"


def test_snapshot_none_name_attrs() -> None:
    snap = build_snapshot(None, probed=True, probe_resolution="1080p")
    assert snap.resolution == "1080p" and snap.media_source is None and snap.remux is False


# ---------------------------------------------------------------------------
# 标签与目标
# ---------------------------------------------------------------------------


def test_labels_and_target() -> None:
    assert quality_label(_snap(resolution="1080p", media_source="WEB-DL")) == "1080p WEB-DL"
    assert quality_label(_snap(resolution="1080p", remux=True)) == "1080p Remux"
    assert quality_label(_snap(resolution="1080p")) == "1080p 片源未知"
    assert quality_label(_snap(media_source="WEB-DL")) == "分辨率未知 WEB-DL"

    assert upgrade_target_label(_spec()) is None
    # 目标分辨率：cutoff > resolutions 首选 > 1080p 兜底
    assert upgrade_target_label(_spec(upgrade_source="remux")) == "1080p Remux"
    assert (
        upgrade_target_label(_spec(upgrade_source="blu-ray", resolutions=["2160p", "1080p"]))
        == "2160p 蓝光"
    )
    assert (
        upgrade_target_label(
            _spec(
                upgrade_source="web-dl",
                resolutions=["2160p", "1080p"],
                cutoff_resolution="1080p",
            )
        )
        == "1080p WEB-DL"
    )


def test_spec_rejects_cutoff_resolution_outside_allowed() -> None:
    """洗版目标分辨率必须在允许列表内，否则永远洗不到。"""
    with pytest.raises(ValueError):
        _spec(resolutions=["1080p"], cutoff_resolution="2160p", upgrade_source="remux")


def test_spec_old_json_reads_as_upgrade_disabled() -> None:
    """旧 spec JSON（无洗版字段）读出即"不洗版"——向前兼容。"""
    spec = RuleSetSpec.model_validate({"resolutions": ["1080p"], "free_only": True})
    assert spec.upgrade_source is None and spec.cutoff_resolution is None
