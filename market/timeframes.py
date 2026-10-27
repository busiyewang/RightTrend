"""周期时长与开盘边界。

OKX 1H/4H 按 UTC 整点对齐(4H 同时也对齐 UTC+8);1D/1W 默认按 UTC+8 开盘,
1Dutc/1Wutc 按 UTC。周线从周一开盘。所有时间均为 UTC 毫秒。
"""

from __future__ import annotations

from typing import Optional

LEVELS = ["1H", "4H", "1D", "1W"]
HOUR_MS = 3600_000
DURATION_MS = {"1H": HOUR_MS, "4H": 4 * HOUR_MS, "1D": 24 * HOUR_MS, "1W": 7 * 24 * HOUR_MS}
_MONDAY_SHIFT_MS = 4 * 24 * HOUR_MS   # 1970-01-01 是周四,+4 天对齐到周一


def period_open(level: str, t_ms: int, daily_offset_ms: int = 8 * HOUR_MS) -> int:
    """t_ms 所在那根K线的开盘时间。"""
    dur = DURATION_MS[level]
    if level in ("1H", "4H"):
        return t_ms // dur * dur
    shift = daily_offset_ms - (_MONDAY_SHIFT_MS if level == "1W" else 0)
    return (t_ms + shift) // dur * dur - shift


def expected_last_closed_open(level: str, now_ms: int, daily_offset_ms: int = 8 * HOUR_MS) -> int:
    """此刻应当已经收盘的最后一根K线的开盘时间。"""
    return period_open(level, now_ms, daily_offset_ms) - DURATION_MS[level]


def bars_between(level: str, start_open: int, end_open: int) -> int:
    return max(0, (end_open - start_open) // DURATION_MS[level])


def close_time(level: str, open_ms: int) -> int:
    return open_ms + DURATION_MS[level]


def level_of(okx_bar: str) -> Optional[str]:
    b = okx_bar.replace("utc", "")
    return b if b in DURATION_MS else None
