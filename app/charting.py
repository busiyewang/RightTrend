"""按需图表快照:行情及本地结构计算,不运行任务、不调用模型、不落盘K线。"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from typing import Callable, Dict, MutableMapping, Optional

from analysis.evidence import level_evidence
from app.config import Settings
from market.service import LevelData, MarketService, check_quality, now_ms
from market.timeframes import DURATION_MS, LEVELS


class ChartUnavailable(RuntimeError):
    def __init__(self, quality: Dict):
        super().__init__("暂时没有可用的已收盘行情,请稍后重试")
        self.quality = quality


class ChartService:
    """Web 进程自有缓存;只预热用户请求的一个周期。

    同一把锁覆盖读取、抓取和计算,并发刷新不会重复请求交易所。缓存最多
    max_entries 个快照,淘汰币种时一并释放其原始K线。时钟、行情服务及快照
    缓存均可注入,离线测试不需要启动 worker 或模型客户端。
    """

    def __init__(self, settings: Settings, market: Optional[MarketService] = None,
                 payload_cache: Optional[MutableMapping] = None,
                 clock: Callable[[], int] = now_ms, ttl_ms: int = 60_000,
                 max_entries: int = 32):
        self.s = settings
        self.market = market if market is not None else MarketService(settings)
        self.cache = payload_cache if payload_cache is not None else OrderedDict()
        self.clock = clock
        self.ttl_ms = ttl_ms
        self.max_entries = max(1, max_entries)
        self._lock = threading.RLock()

    def snapshot(self, inst_id: str, level: str) -> Dict:
        if level not in LEVELS:
            raise ValueError("图表周期只能是 1H、4H、1D 或 1W")
        key = (inst_id, level)
        with self._lock:
            now = self.clock()
            previous = self.cache.get(key)
            if previous and 0 <= now - previous["cached_at"] < self.ttl_ms:
                # 移至末尾以保留最近访问的快照;返回副本以免调用方修改缓存。
                self.cache.pop(key)
                self.cache[key] = previous
                result = copy.deepcopy(previous["payload"])
                result["cache_hit"] = True
                return result

            self.market.update(inst_id, level, now)
            as_of = self.clock()
            source = self.market.level_data(inst_id, level, as_of)
            entry = self.market.cache.entry(inst_id, level)
            result = self._build(inst_id, source, as_of, entry.last_error if entry else None)
            if not result["candles"]:
                if previous:
                    # 即便底层缓存不可用,最后一份可用快照仍可查看,但明确过期。
                    result = copy.deepcopy(previous["payload"])
                    result["quality"] = check_quality(
                        level, result["candles"], as_of, self.s.min_bars.get(level, 30),
                        self.s.daily_offset_ms, last_error="本轮行情不可用,显示上次图表快照")
                    result["quality"]["status"] = "stale"
                    result["quality"]["cache_fallback"] = True
                    result["cache_hit"] = True
                    result["served_at"] = as_of
                    return result
                # 空结果不保存60秒负缓存,用户点击重试可立即重新抓取。
                if not any(k[0] == inst_id for k in self.cache):
                    self.market.cache.drop(inst_id)
                raise ChartUnavailable(result["quality"])

            self.cache.pop(key, None)
            self.cache[key] = {"cached_at": as_of, "payload": result}
            while len(self.cache) > self.max_entries:
                oldest = next(iter(self.cache))
                self.cache.pop(oldest)
                if not any(k[0] == oldest[0] for k in self.cache):
                    self.market.cache.drop(oldest[0])
            return copy.deepcopy(result)

    def _build(self, inst_id: str, source: LevelData, as_of: int,
               last_error: Optional[str]) -> Dict:
        duration = DURATION_MS[source.level]
        # 交易所确认标志与本地收盘边界必须同时满足;不能只信 confirm=1。
        closed = tuple(c for c in source.candles
                       if c.get("confirm") == 1 and c["ts"] + duration <= as_of)
        forming = [c for c in source.candles if c.get("confirm") != 1
                   and c["ts"] <= as_of < c["ts"] + duration]
        quality = check_quality(
            source.level, closed, as_of, self.s.min_bars.get(source.level, 30),
            self.s.daily_offset_ms,
            history_exhausted=source.quality.get("history_exhausted", False),
            last_error=last_error)
        excluded = sum(1 for c in source.candles
                       if c.get("confirm") == 1 and c["ts"] + duration > as_of)
        if excluded:
            quality["issues"].append(f"已排除 {excluded} 根尚未到收盘时间的确认K线")
            if quality["status"] == "ok":
                quality["status"] = "stale"
        quality["excluded_unclosed_confirmed"] = excluded
        quality["refresh_failed"] = bool(last_error)
        quality["cache_fallback"] = bool(last_error and closed)
        clean = LevelData(source.level, source.okx_bar, closed, source.fetched_at, quality)
        evidence = level_evidence(clean, closed[-1]["c"] if closed else None,
                                  self.s.stop_buffer)
        payload = evidence["payload"]
        return {"code": 0, "inst_id": inst_id, "level": source.level,
                "analysis_as_of": as_of, "fetched_at": source.fetched_at,
                "daily_bar_mode": self.s.daily_bar_mode, "okx_bar": source.okx_bar,
                "quality": quality, "candles": payload["candles"],
                "indicators": payload["indicators"], "chan": payload["chan"],
                "signals": payload["signals"], "evidence": evidence["evidence"],
                "key_levels": evidence["key_levels"],
                "forming_bar": forming[-1] if forming else None, "cache_hit": False}
