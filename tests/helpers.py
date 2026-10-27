"""离线夹具:合成K线、假 OKX 客户端、假 DeepSeek HTTP 会话。不访问网络。"""

from __future__ import annotations

import json
import math
from typing import Dict, List, Optional

from app.config import Settings, load_settings
from market.timeframes import DURATION_MS, LEVELS, expected_last_closed_open, period_open

NOW = 1793098890000          # 2026-10-27 11:01:30 UTC(整点后 90 秒)


def settings(**kw) -> Settings:
    base = dict(db_path=":memory:", deepseek_api_key="", deepseek_model="")
    base.update(kw)
    return load_settings(env_file="/nonexistent", **base)


def series(level: str, now: int, n: int, base: float = 100.0, seed: int = 1,
           offset_ms: int = 8 * 3600_000, forming: bool = True) -> List[Dict]:
    """确定性的多周期正弦波K线,升序;最后一根为未收盘(confirm=0)。"""
    dur = DURATION_MS[level]
    last_closed = expected_last_closed_open(level, now, offset_ms)
    start = last_closed - (n - 1) * dur
    out = []
    for i in range(n + (1 if forming else 0)):
        t = start + i * dur
        x = i + seed * 7
        mid = base * (1 + 0.08 * math.sin(x / 9.0) + 0.04 * math.sin(x / 3.1) + 0.002 * i / 10)
        o = mid * (1 - 0.003 * math.sin(x))
        c = mid * (1 + 0.003 * math.sin(x * 1.7))
        h = max(o, c) * 1.004
        l = min(o, c) * 0.996
        out.append({"ts": t, "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4),
                    "vol": 1000 + 100 * (i % 7), "vol_ccy": 10.0 + i % 5, "vol_quote": 1e5,
                    "confirm": 0 if (forming and i == n) else 1})
    assert period_open(level, out[0]["ts"], offset_ms) == out[0]["ts"]
    return out


class FakeOkx:
    """按 OKX 语义返回:倒序;recent 取最新 limit 根;history(after=ts) 返回早于 ts 的。"""
    RECENT_LIMIT = 300
    HISTORY_LIMIT = 100

    def __init__(self, data: Dict[str, List[Dict]]):
        self.data = data            # okx_bar → 升序K线
        self.calls: List = []

    def recent(self, inst_id, bar, limit=300, after=None):
        self.calls.append(("recent", bar, limit))
        rows = self.data[bar]
        if after:
            rows = [c for c in rows if c["ts"] < after]
        return [dict(c) for c in reversed(rows[-min(limit, 300):])]

    def history(self, inst_id, bar, after, limit=100):
        self.calls.append(("history", bar, after))
        rows = [c for c in self.data[bar] if c["ts"] < after]
        return [dict(c) for c in reversed(rows[-min(limit, 100):])]


def market_data(now: int = NOW, n: int = 400) -> Dict[str, List[Dict]]:
    return {lv: series(lv, now, n if lv != "1W" else 120, seed=i + 1) for i, lv in enumerate(LEVELS)}


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, text=None, url="https://x/"):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(body or {})
        self.url = url

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def chat_body(content: Optional[str], finish="stop", model="test-model"):
    return {"model": model, "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"total_tokens": 1234}}


class FakeSession:
    """依次返回预设响应;元素可以是异常实例(模拟超时)。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.headers = {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
