from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings


_KST = timezone(timedelta(hours=9))


def resolve_timezone(name: str) -> tzinfo:
    raw = str(name or "").strip()
    if not raw:
        return timezone.utc

    upper = raw.upper()
    if upper in {"KST", "ASIA/SEOUL", "SEOUL"}:
        try:
            return ZoneInfo("Asia/Seoul")
        except ZoneInfoNotFoundError:
            return _KST
    if upper == "UTC":
        return timezone.utc

    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return timezone.utc


def business_tz() -> tzinfo:
    return resolve_timezone(settings.business_tz)
