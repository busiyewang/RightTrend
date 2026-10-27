"""行情服务:首次预热、之后增量合并,产出同一分析批次的不可变四周期快照。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.config import Settings
from market.cache import CandleCache
from market.okx import OkxClient, OkxError
from market.timeframes import (DURATION_MS, LEVELS, bars_between, expected_last_closed_open,
                               period_open)

OVERLAP_BARS = 3            # 增量更新与已有数据保留的重叠根数
MAX_BACKFILL_PAGES = 30     # 单次补缺口/预热的历史分页上限


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class LevelData:
    level: str
    okx_bar: str
    candles: Tuple[Dict, ...]           # 升序,含最后一根未收盘
    fetched_at: Optional[int]
    quality: Dict

    @property
    def confirmed(self) -> List[Dict]:
        return [c for c in self.candles if c.get("confirm") == 1]

    @property
    def forming(self) -> Optional[Dict]:
        return self.candles[-1] if self.candles and self.candles[-1].get("confirm") != 1 else None


@dataclass(frozen=True)
class MarketBatch:
    inst_id: str
    analysis_as_of: int
    levels: Dict[str, LevelData]

    @property
    def live_price(self) -> Optional[float]:
        lv = self.levels.get("1H")
        return lv.candles[-1]["c"] if lv and lv.candles else None

    def meta(self) -> Dict:
        """可落库的批次摘要(不含K线数组)。"""
        return {"inst_id": self.inst_id, "analysis_as_of": self.analysis_as_of,
                "live_price": self.live_price,
                "levels": {k: {"okx_bar": v.okx_bar, "fetched_at": v.fetched_at, **v.quality}
                           for k, v in self.levels.items()}}


def check_quality(level: str, candles, now: int, min_bars: int, daily_offset_ms: int,
                  history_exhausted: bool = False, last_error: Optional[str] = None) -> Dict:
    confirmed = [c for c in candles if c.get("confirm") == 1]
    dur = DURATION_MS[level]
    expected = expected_last_closed_open(level, now, daily_offset_ms)
    q: Dict = {"count": len(candles), "confirmed_count": len(confirmed),
               "expected_last_closed_open": expected,
               "last_closed_open": confirmed[-1]["ts"] if confirmed else None,
               "last_closed_at": confirmed[-1]["ts"] + dur if confirmed else None,
               "gaps": 0, "misaligned": 0, "issues": []}
    if not confirmed:
        q["status"] = "error" if last_error else "empty"
        if last_error:
            q["issues"].append(f"抓取失败: {last_error}")
        q["issues"].append("没有已收盘K线")
        return q
    for a, b in zip(confirmed, confirmed[1:]):
        if b["ts"] - a["ts"] != dur:
            q["gaps"] += 1
    for c in confirmed[-50:]:
        if period_open(level, c["ts"], daily_offset_ms) != c["ts"]:
            q["misaligned"] += 1
    status = "ok"
    if confirmed[-1]["ts"] < expected:
        lag = bars_between(level, confirmed[-1]["ts"], expected)
        q["issues"].append(f"最后已收盘K线落后应收盘边界 {lag} 根")
        status = "stale"
    if q["misaligned"]:
        q["issues"].append(f"{q['misaligned']} 根K线开盘时间与 {level} 口径不一致(检查 1D/1W 时区口径)")
        status = "stale" if status == "ok" else status
    if len(confirmed) < min_bars:
        q["issues"].append(f"已收盘K线 {len(confirmed)} 根,少于最低分析根数 {min_bars}"
                           + ("(交易所历史已到底,新币)" if history_exhausted else ""))
        status = "insufficient"
    if q["gaps"]:
        q["issues"].append(f"已收盘序列有 {q['gaps']} 处缺口")
    if last_error:
        q["issues"].append(f"最近一次抓取失败,使用旧缓存: {last_error}")
        if status == "ok":
            status = "stale"
    q["history_exhausted"] = history_exhausted
    q["status"] = status
    return q


class MarketService:
    def __init__(self, settings: Settings, client: Optional[OkxClient] = None,
                 cache: Optional[CandleCache] = None):
        self.s = settings
        self.client = client or OkxClient(settings.okx_base, settings.http_timeout)
        self.cache = cache or CandleCache(settings.cache_max_bars)
        self._inst_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _inst_lock(self, inst_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._inst_locks.setdefault(inst_id, threading.Lock())

    # ---------- 抓取 ----------
    def _warmup(self, inst_id: str, level: str, now: int):
        bar = self.s.okx_bar(level)
        rows = self.client.recent(inst_id, bar, OkxClient.RECENT_LIMIT)
        got = list(rows)
        pages = 0
        while got and len(got) < self.s.warmup_bars and pages < MAX_BACKFILL_PAGES:
            oldest = min(c["ts"] for c in got)
            page = self.client.history(inst_id, bar, after=oldest)
            pages += 1
            if not page:
                self.cache.mark_exhausted(inst_id, level)
                break
            got.extend(page)
        else:
            if not got:
                self.cache.mark_exhausted(inst_id, level)
        self.cache.merge(inst_id, level, got, fetched_at=now)

    def _incremental(self, inst_id: str, level: str, now: int, last_ts: int):
        bar = self.s.okx_bar(level)
        cur_open = period_open(level, now, self.s.daily_offset_ms)
        missing = bars_between(level, last_ts, cur_open) + 1
        rows = self.client.recent(inst_id, bar, missing + OVERLAP_BARS)
        got = list(rows)
        pages = 0
        # 长时间离线:近期接口不足以覆盖到已有最新K线,继续向前分页补缺口
        while got and min(c["ts"] for c in got) > last_ts and pages < MAX_BACKFILL_PAGES:
            page = self.client.history(inst_id, bar, after=min(c["ts"] for c in got))
            pages += 1
            if not page:
                break
            got.extend(page)
        self.cache.merge(inst_id, level, got, fetched_at=now)

    def update(self, inst_id: str, level: str, now: Optional[int] = None):
        now = now or now_ms()
        e = self.cache.entry(inst_id, level)
        try:
            if not e or not e.by_ts:
                self._warmup(inst_id, level, now)
            else:
                self._incremental(inst_id, level, now, max(e.by_ts))
        except (OkxError, OSError) as ex:
            self.cache.set_error(inst_id, level, str(ex))

    def warmup(self, inst_ids: List[str]):
        for inst in inst_ids:
            with self._inst_lock(inst):
                for lv in LEVELS:
                    self.update(inst, lv)

    def level_data(self, inst_id: str, level: str, now: int) -> LevelData:
        e = self.cache.entry(inst_id, level)
        candles = self.cache.snapshot(inst_id, level)
        q = check_quality(level, candles, now, self.s.min_bars.get(level, 30),
                          self.s.daily_offset_ms,
                          history_exhausted=bool(e and e.history_exhausted),
                          last_error=e.last_error if e else None)
        return LevelData(level, self.s.okx_bar(level), candles, e.fetched_at if e else None, q)

    def batch(self, inst_id: str, now: Optional[int] = None, refresh: bool = True) -> MarketBatch:
        """更新四个周期并在同一把锁内取快照,保证模型看到的是同一批次数据。"""
        now = now or now_ms()
        with self._inst_lock(inst_id):
            if refresh:
                for lv in LEVELS:
                    self.update(inst_id, lv, now)
            levels = {lv: self.level_data(inst_id, lv, now) for lv in LEVELS}
        return MarketBatch(inst_id, now, levels)
