"""原始K线内存缓存:键 (inst_id, level),按开盘时间去重排序,容量有界。

只存在进程内存;不写数据库、文件或日志。重启后由 MarketService 重新预热。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class CacheEntry:
    by_ts: Dict[int, Dict] = field(default_factory=dict)
    fetched_at: Optional[int] = None      # 最近一次成功抓取(UTC ms)
    history_exhausted: bool = False       # 已翻到交易所最早数据(新币)
    last_error: Optional[str] = None
    revisions: int = 0                    # 同一根K线被覆盖修订的次数(含未收盘→收盘)

    def sorted(self) -> List[Dict]:
        return [self.by_ts[t] for t in sorted(self.by_ts)]


class CandleCache:
    def __init__(self, max_bars: int = 1200):
        self.max_bars = max_bars
        self._entries: Dict[Tuple[str, str], CacheEntry] = {}
        self._lock = threading.RLock()

    def lock(self):
        return self._lock

    def entry(self, inst_id: str, level: str) -> Optional[CacheEntry]:
        with self._lock:
            return self._entries.get((inst_id, level))

    def merge(self, inst_id: str, level: str, candles: Iterable[Dict],
              fetched_at: Optional[int] = None) -> Dict[str, int]:
        """合并一批K线(任意顺序、可重叠)。同一开盘时间以新数据覆盖;
        已收盘K线不会被未收盘版本回退覆盖。超过容量时丢弃最旧的。"""
        added = updated = 0
        with self._lock:
            e = self._entries.setdefault((inst_id, level), CacheEntry())
            for c in candles:
                ts = int(c["ts"])
                old = e.by_ts.get(ts)
                if old is None:
                    e.by_ts[ts] = dict(c)
                    added += 1
                    continue
                if old.get("confirm") == 1 and c.get("confirm") != 1:
                    continue
                if old != c:
                    e.by_ts[ts] = dict(c)
                    e.revisions += 1
                    updated += 1
            dropped = 0
            if len(e.by_ts) > self.max_bars:
                for ts in sorted(e.by_ts)[: len(e.by_ts) - self.max_bars]:
                    del e.by_ts[ts]
                    dropped += 1
            if fetched_at is not None:
                e.fetched_at = fetched_at
                e.last_error = None
        return {"added": added, "updated": updated, "dropped": dropped}

    def set_error(self, inst_id: str, level: str, err: str):
        with self._lock:
            self._entries.setdefault((inst_id, level), CacheEntry()).last_error = err[:300]

    def mark_exhausted(self, inst_id: str, level: str):
        with self._lock:
            self._entries.setdefault((inst_id, level), CacheEntry()).history_exhausted = True

    def snapshot(self, inst_id: str, level: str) -> Tuple[Dict, ...]:
        """升序K线的不可变副本(tuple + 逐根拷贝),供同一分析批次使用。"""
        with self._lock:
            e = self._entries.get((inst_id, level))
            return tuple(dict(c) for c in e.sorted()) if e else tuple()

    def keys(self) -> List[Tuple[str, str]]:
        with self._lock:
            return list(self._entries)

    def drop(self, inst_id: str):
        with self._lock:
            for k in [k for k in self._entries if k[0] == inst_id]:
                del self._entries[k]

    def stats(self) -> List[Dict]:
        """只给出计数与时间,不暴露K线数组。"""
        with self._lock:
            out = []
            for (inst, lvl), e in sorted(self._entries.items()):
                ts = sorted(e.by_ts)
                out.append({"inst_id": inst, "level": lvl, "count": len(ts),
                            "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
                            "fetched_at": e.fetched_at, "history_exhausted": e.history_exhausted,
                            "last_error": e.last_error})
            return out
