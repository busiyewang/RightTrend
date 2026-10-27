"""OKX 公共行情客户端(无需 key)。

- /api/v5/market/candles:最近数据,limit ≤ 300,可带 after 向更早翻页;
- /api/v5/market/history-candles:更早历史,limit ≤ 100,after=ts 返回早于 ts 的K线。
两者都按时间倒序返回 [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]。
限速:candles 40 次/2s、history-candles 20 次/2s(按 IP),这里统一做最小间隔。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import requests


class OkxError(RuntimeError):
    pass


def parse_rows(rows) -> List[Dict]:
    out = []
    for r in rows:
        out.append({
            "ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
            "c": float(r[4]),
            "vol": float(r[5]),                 # 合约张数(SWAP)/ 基础币数量(现货)
            "vol_ccy": float(r[6]) if r[6] not in ("", None) else None,   # 基础币数量
            "vol_quote": float(r[7]) if len(r) > 7 and r[7] not in ("", None) else None,  # 计价币
            "confirm": int(r[8]) if len(r) > 8 else 0,
        })
    return out


class OkxClient:
    RECENT_LIMIT = 300
    HISTORY_LIMIT = 100

    def __init__(self, base: str = "https://www.okx.com", timeout: float = 15.0,
                 min_interval: float = 0.12, session: Optional[requests.Session] = None):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.sess = session or requests.Session()
        self._lock = threading.Lock()
        self._last = 0.0
        self.request_count = 0

    def _get(self, path: str, params: Dict) -> List:
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                self.request_count += 1
                r = self.sess.get(self.base + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.0 + attempt)
                    last_err = OkxError("OKX 限流 429")
                    continue
                body = r.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
                continue
            if body.get("code") != "0":
                if body.get("code") == "50011":      # 请求过于频繁
                    time.sleep(1.0 + attempt)
                    last_err = OkxError(body.get("msg") or "OKX 限流")
                    continue
                raise OkxError(f"OKX 返回错误 {body.get('code')}: {body.get('msg')}")
            return body.get("data") or []
        raise OkxError(f"OKX 请求失败: {last_err}")

    def recent(self, inst_id: str, bar: str, limit: int = RECENT_LIMIT,
               after: Optional[int] = None) -> List[Dict]:
        params = {"instId": inst_id, "bar": bar, "limit": str(min(limit, self.RECENT_LIMIT))}
        if after:
            params["after"] = str(after)
        return parse_rows(self._get("/api/v5/market/candles", params))

    def history(self, inst_id: str, bar: str, after: int,
                limit: int = HISTORY_LIMIT) -> List[Dict]:
        params = {"instId": inst_id, "bar": bar, "after": str(after),
                  "limit": str(min(limit, self.HISTORY_LIMIT))}
        return parse_rows(self._get("/api/v5/market/history-candles", params))
