"""片源档统一表的等价性测试（quality-upgrade.md Phase 7）。

换源 `_SOURCE_RANK` 改为从 matcher 的统一片源档阶梯派生后，旧表覆盖的
所有值必须保持原有相对序（行为等价），新补全的档位按新语义生效。
"""

from __future__ import annotations

import pytest
from movieclaw_api.services.subscription.replacement import (
    _SOURCE_RANK,
    _source_rank,
    quality_not_lower,
)
from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import TorrentCandidate


def _candidate(**attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="t", torrent_id="1", title="", subtitle="", attrs=TorrentAttrs(**attrs)
    )


# 旧表（迁移前的字面值），等价性以它的相对序为准
_LEGACY = {
    "hdtv": 10,
    "web-rip": 20,
    "webrip": 20,
    "web-dl": 30,
    "webdl": 30,
    "blu-ray": 40,
    "bluray": 40,
    "uhd blu-ray": 50,
    "uhd bluray": 50,
}


def test_legacy_relative_order_preserved() -> None:
    """旧表覆盖的每一对值，新表的大小关系与旧表完全一致。"""
    keys = list(_LEGACY)
    for a in keys:
        for b in keys:
            legacy = (_LEGACY[a] > _LEGACY[b]) - (_LEGACY[a] < _LEGACY[b])
            unified = (_SOURCE_RANK[a] > _SOURCE_RANK[b]) - (
                _SOURCE_RANK[a] < _SOURCE_RANK[b]
            )
            assert legacy == unified, f"{a} vs {b} 相对序改变"


def test_previously_unknown_sources_now_ranked() -> None:
    """旧表的缺口（BDRip/HDRip/DVD 等）补齐：不再被当作未知。"""
    assert _source_rank("BDRip") is not None
    assert _source_rank("HDRip") is not None
    assert _source_rank("DVD") is not None
    # 档位关系符合统一阶梯：Rip 类 < WEB-DL < 蓝光
    assert _source_rank("BDRip") < _source_rank("WEB-DL") < _source_rank("Blu-ray")
    # 子串容错与长键优先：DVDRip 不被 DVD 短键误吞、HDTVRip 同理
    assert _source_rank("DVDRip") == _source_rank("BDRip")
    assert _source_rank("HDTVRip") == _source_rank("HDTV")


@pytest.mark.parametrize(
    "old, new, expected",
    [
        # 迁移前后行为必须一致的核心矩阵
        ({"media_source": "WEB-DL", "resolution": "1080p"},
         dict(media_source="Blu-ray", resolution="1080p"), True),
        ({"media_source": "Blu-ray", "resolution": "1080p"},
         dict(media_source="WEB-DL", resolution="1080p"), False),
        ({"media_source": "UHD Blu-ray", "resolution": "2160p"},
         dict(media_source="Blu-ray", resolution="2160p"), False),  # UHD 半档保护
        ({"media_source": "WEB-DL", "resolution": "2160p"},
         dict(media_source="WEB-DL", resolution="1080p"), False),  # 分辨率降级
        ({"remux": True, "media_source": "Blu-ray", "resolution": "1080p"},
         dict(media_source="Blu-ray", resolution="1080p"), False),  # Remux 底线
        ({}, dict(media_source="Blu-ray", resolution="2160p"), False),  # 无基线拒绝
    ],
)
def test_quality_not_lower_behavior(old, new, expected) -> None:
    assert quality_not_lower(_candidate(**new), old) is expected
