"""本地证据:对每个级别的已收盘K线跑缠论与指标,产出带编号的证据和关键位。

- 证据编号形如 E-1H-BSP-1793000000000-B2、E-4H-ZS-1792900000000;
  关键位编号形如 L-4H-ZG、L-1H-BSP-1793000000000-B2、L-1D-RECENT_HIGH。
- 结构只用 confirm=1 的K线;未收盘K线只放在 forming_bar 里作风险检查。
- 每条信号保留 known_at / locked_at(事件K线收盘时间),校验层据此判断
  "分析时刻之前是否已可知并锁定",不能用极值所在K线时间替代。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from analysis.chanlun import chan, decision, indicators
from market.timeframes import LEVELS

LEVEL_UP = {"1H": "4H", "4H": "1D", "1D": "1W", "1W": "1W"}
RECENT_WINDOW = 120


def _r(v, nd=6):
    if v is None:
        return None
    return float(f"{v:.{nd}g}")


def _payload(level_data) -> Dict:
    """仿照参考项目 build_payload,但 candles 只含已收盘K线(本地决策的"现价"为最后收盘价)。"""
    confirmed = level_data.confirmed
    if not confirmed:
        return {"candles": [], "chan": {"bi": [], "bsp": [], "beichi": [], "beili": [],
                                        "zhongshu_display": [], "summary": {}},
                "indicators": None, "signals": {}}
    analysis = chan.analyze(confirmed, level_data.level)
    inds = indicators.compute_all(confirmed)
    sig = indicators.analyze_signals(confirmed, analysis.get("bi"))
    return {"candles": confirmed, "chan": analysis, "indicators": inds, "signals": sig}


def _signal_invalidated(sig: Dict, candles: List[Dict], live: Optional[float], buffer: float) -> Optional[str]:
    """买点:其后任一已收盘K线收盘或现价跌破信号价(含缓冲)即失效;卖点对称。"""
    buy = sig["type"][0] == "B"
    p = sig["price"]
    after = [c for c in candles if c["ts"] > sig["ts"]]
    if buy:
        broken = [c for c in after if c["c"] < p * (1 - buffer)]
        if broken:
            return f"其后已收盘K线收于 {broken[0]['c']},跌破信号价 {p}"
        if live is not None and live < p * (1 - buffer):
            return f"现价 {live} 已跌破信号价 {p}"
    else:
        broken = [c for c in after if c["c"] > p * (1 + buffer)]
        if broken:
            return f"其后已收盘K线收于 {broken[0]['c']},突破信号价 {p}"
        if live is not None and live > p * (1 + buffer):
            return f"现价 {live} 已突破信号价 {p}"
    return None


def level_evidence(level_data, live: Optional[float], stop_buffer: float = 0.002) -> Dict:
    lv = level_data.level
    pl = _payload(level_data)
    ch = pl["chan"]
    cs = pl["candles"]
    out: Dict = {"level": lv, "quality": level_data.quality, "evidence": [], "key_levels": [],
                 "payload": pl}
    if not cs:
        return out
    fin = [b for b in ch["bi"] if not b.get("unfinished")]
    cutoff = decision.active_signal_cutoff(fin) if fin else float("inf")
    ev, kl = out["evidence"], out["key_levels"]

    for p in ch.get("bsp") or []:
        eid = f"E-{lv}-BSP-{p['ts']}-{p['type']}"
        inv = _signal_invalidated(p, cs, live, stop_buffer)
        ev.append({"id": eid, "kind": "bsp", "level": lv, "type": p["type"],
                   "price": _r(p["price"]), "anchor_ts": p["ts"],
                   "known_at": p.get("known_at"), "locked": bool(p.get("locked")),
                   "locked_at": p.get("locked_at"), "active": p["k_idx"] >= cutoff,
                   "invalidated": inv, "score": p.get("score"), "grade": p.get("grade"),
                   "note": p.get("note"), "confirms": p.get("confirms")})
        kl.append({"id": f"L-{lv}-BSP-{p['ts']}-{p['type']}", "level": lv,
                   "name": f"{lv} {p['type']} 信号价", "price": _r(p["price"])})

    centers = ch.get("zhongshu_display") or []
    for z in centers[-3:]:
        ev.append({"id": f"E-{lv}-ZS-{z.get('start_ts')}", "kind": "center", "level": lv,
                   "zg": _r(z["zg"]), "zd": _r(z["zd"]), "gg": _r(z.get("gg")), "dd": _r(z.get("dd")),
                   "known_at": z.get("known_at"), "locked": bool(z.get("locked")),
                   "locked_at": z.get("locked_at"), "bi_count": z.get("bi_count")})
    if centers:
        z = centers[-1]
        for name, key in (("ZG", "zg"), ("ZD", "zd"), ("GG", "gg"), ("DD", "dd")):
            if z.get(key) is not None:
                kl.append({"id": f"L-{lv}-{name}", "level": lv, "name": f"{lv} 最近中枢 {name}",
                           "price": _r(z[key])})

    for b in ch.get("beichi", [])[-3:]:
        ev.append({"id": f"E-{lv}-BC-{b.get('ts')}", "kind": "beichi", "level": lv,
                   "beichi_kind": b.get("kind"), "dir": b.get("dir"), "struct_ok": b.get("struct_ok"),
                   "price": _r(b.get("price")), "area_ratio": b.get("area_ratio"),
                   "known_at": b.get("known_at"), "locked": bool(b.get("locked")),
                   "locked_at": b.get("locked_at")})
    for b in ch.get("beili", [])[-3:]:
        ev.append({"id": f"E-{lv}-DL-{b.get('ts')}-{b.get('type')}", "kind": "beili", "level": lv,
                   "type": b.get("type"), "subtype": b.get("subtype"), "price": _r(b.get("price")),
                   "votes": b.get("votes"), "known_at": b.get("known_at"),
                   "locked": bool(b.get("locked")), "locked_at": b.get("locked_at")})
    for b in fin[-6:]:
        ev.append({"id": f"E-{lv}-BI-{b.get('start_ts')}", "kind": "bi", "level": lv,
                   "dir": b["dir"], "from": [b.get("start_ts"), _r(b["start_price"])],
                   "to": [b.get("end_ts"), _r(b["end_price"])], "state": b.get("state"),
                   "locked": bool(b.get("locked")), "known_at": b.get("known_at"),
                   "locked_at": b.get("locked_at")})

    recent = cs[-RECENT_WINDOW:]
    kl.append({"id": f"L-{lv}-RECENT_HIGH", "level": lv, "name": f"{lv} 近{len(recent)}根最高",
               "price": _r(max(c["h"] for c in recent))})
    kl.append({"id": f"L-{lv}-RECENT_LOW", "level": lv, "name": f"{lv} 近{len(recent)}根最低",
               "price": _r(min(c["l"] for c in recent))})
    kl.append({"id": f"L-{lv}-LAST_CLOSE", "level": lv, "name": f"{lv} 最后收盘价",
               "price": _r(cs[-1]["c"])})
    # 只添加可核验的已收盘均线价位,不创建或升级任何结构信号。
    for n in indicators.EMA_PERIODS:
        values = pl["indicators"]["ema"][f"ema{n}"]
        if values[-1] is not None:
            kl.append({"id": f"L-{lv}-EMA{n}", "level": lv,
                       "name": f"{lv} EMA{n}(已收盘)", "price": _r(values[-1])})
    return out


def build_evidence(batch, stop_buffer: float = 0.002) -> Dict:
    live = batch.live_price
    levels = {lv: level_evidence(batch.levels[lv], live, stop_buffer) for lv in LEVELS}
    for lv in LEVELS:
        cur, hi = levels[lv]["payload"], levels[LEVEL_UP[lv]]["payload"]
        if cur["candles"] and hi["candles"]:
            try:
                levels[lv]["local_decision"] = decision.decide(cur, hi, lv, LEVEL_UP[lv])
            except Exception as e:   # 本地决策只作参考证据,异常不阻断
                levels[lv]["local_decision"] = {"action": "error", "reason": str(e)[:200]}
            levels[lv]["direction"] = decision._direction(cur["chan"].get("summary") or {})
        else:
            levels[lv]["local_decision"] = {"action": "unavailable", "reason": "数据不足"}
            levels[lv]["direction"] = {"dir": "unknown", "strength": 0, "desc": "数据不足"}
    index = {e["id"]: e for lv in LEVELS for e in levels[lv]["evidence"]}
    key_levels = {k["id"]: k for lv in LEVELS for k in levels[lv]["key_levels"]}
    key_levels["LIVE"] = {"id": "LIVE", "level": "1H", "name": "现价(含未收盘)", "price": live}
    return {"levels": levels, "index": index, "key_levels": key_levels, "live_price": live,
            "analysis_as_of": batch.analysis_as_of}
