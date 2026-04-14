from __future__ import annotations

import os
import logging
import sys
from datetime import datetime, timedelta, timezone


def _parse_log_tz() -> timezone:
    """Resolve logging timezone from env.

    Priority:
    1) LOG_TZ_OFFSET like +09:00 / -05:30
    2) LOG_TZ (supports UTC, KST, Asia/Seoul)
    """

    offset = os.getenv("LOG_TZ_OFFSET", "").strip()
    if offset:
        sign = 1
        s = offset
        if s.startswith("+"):
            s = s[1:]
        elif s.startswith("-"):
            sign = -1
            s = s[1:]
        parts = s.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            hh = int(parts[0])
            mm = int(parts[1])
            return timezone(sign * timedelta(hours=hh, minutes=mm))

    tz_name = os.getenv("LOG_TZ", "UTC").strip().upper()
    if tz_name in ("KST", "ASIA/SEOUL"):
        return timezone(timedelta(hours=9))
    return timezone.utc


class _TZFormatter(logging.Formatter):
    def __init__(self, fmt: str, tz: timezone):
        super().__init__(fmt)
        self._tz = tz

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="milliseconds")


def setup_logging(level: str = "INFO") -> None:
    """Configure basic console logging.

    For a PoC we keep logging minimal. Uvicorn will also configure its own loggers,
    but having a consistent root logger helps when debugging ML/model loading.
    """

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid duplicate handlers if the module is imported multiple times.
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handler.setFormatter(_TZFormatter(fmt, _parse_log_tz()))
    root.addHandler(handler)

    # Quiet noisy loggers if needed.
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
