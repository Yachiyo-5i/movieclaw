"""PT 站点时间契约：站点本地时间统一归一为 naive UTC。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# 当前内置站点均使用中国标准时间。保留站点级 YAML 覆盖能力，后续接入海外站时
# 无需改解析代码；数据库与跨层传递仍一律使用 naive UTC。
DEFAULT_SITE_TIMEZONE = "Asia/Shanghai"


def to_naive_utc(value: datetime, source_timezone: ZoneInfo) -> datetime:
    """把站点返回的时间转成数据库约定的 naive UTC。

    带时区值尊重自身时区；naive 值才按站点声明的本地时区解释。这样 API 站点
    将来若开始返回 ISO offset，也不会被重复套用默认时区。
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=source_timezone)
    return aware.astimezone(UTC).replace(tzinfo=None)
