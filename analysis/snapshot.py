"""模型输入:四周期紧凑表 + 本地证据 + 关键位 + 缺失说明,按字符预算裁剪。

裁剪顺序:先删辅助说明(指标状态、背离、旧笔、本地决策长文案),再按比例缩短
K线历史;四个周期始终保留。裁剪记录写入 input_meta.trim,不做 JSON 字符串截断。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from analysis.chanlun.indicators import EMA_PERIODS
from market.timeframes import LEVELS

TZ8 = timezone(timedelta(hours=8))
COLUMNS = ["time", "open", "high", "low", "close", "vol", "vol_ccy",
           "dif", "dea", "hist", "k", "d", "j", "rsi12"]
MIN_BARS_IN_PROMPT = {"1H": 24, "4H": 24, "1D": 20, "1W": 12}


def fmt_ts(ts_ms: Optional[int], tz=TZ8) -> Optional[str]:
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz).strftime("%Y-%m-%d %H:%M")


def _r(v, nd=6):
    if v is None:
        return None
    try:
        return float(f"{float(v):.{nd}g}")
    except (TypeError, ValueError):
        return None


def _rows(pl: Dict, n: int) -> List[List]:
    cs = pl["candles"]
    ind = pl["indicators"]
    if not cs or not ind:
        return []
    m, k, r = ind["macd"], ind["kdj"], ind["rsi"]
    start = max(0, len(cs) - n)
    rows = []
    for i in range(start, len(cs)):
        c = cs[i]
        rows.append([fmt_ts(c["ts"]), _r(c["o"]), _r(c["h"]), _r(c["l"]), _r(c["c"]),
                     _r(c["vol"], 4), _r(c.get("vol_ccy"), 4),
                     _r(m["dif"][i], 4), _r(m["dea"][i], 4), _r(m["hist"][i], 4),
                     _r(k["k"][i], 3), _r(k["d"][i], 3), _r(k["j"][i], 3), _r(r["rsi12"][i], 3)])
    return rows


def _ev_view(e: Dict, full: bool) -> Dict:
    out = {k: v for k, v in e.items() if v is not None and k not in ("anchor_ts",)}
    for key in ("known_at", "locked_at"):
        if key in out:
            out[key] = fmt_ts(out[key])
    if "anchor_ts" in e:
        out["time"] = fmt_ts(e["anchor_ts"])
    if not full:
        out.pop("note", None)
        out.pop("confirms", None)
    return out


def _level_block(lv: str, item: Dict, n_bars: int, full: bool, as_of: int) -> Dict:
    pl = item["payload"]
    q = item["quality"]
    ev = item["evidence"]
    kinds = ("bsp", "center", "beichi", "beili", "bi") if full else ("bsp", "center", "beichi", "bi")
    evidence = [e for e in ev if e["kind"] in kinds]
    if not full:
        bis = [e for e in evidence if e["kind"] == "bi"][-4:]
        evidence = [e for e in evidence if e["kind"] != "bi"] + bis
    dec = item.get("local_decision") or {}
    keep = ["action", "dir_level", "dir", "q", "entry", "stop", "targets", "rr", "better_entry"]
    if full:
        keep += ["reason", "warnings"]
    block = {
        "quality": {"status": q.get("status"), "confirmed_count": q.get("confirmed_count"),
                    "last_closed_at": fmt_ts(q.get("last_closed_at")), "issues": q.get("issues")},
        "direction": item.get("direction"),
        "bars_sent": 0,
        "bars": _rows(pl, n_bars),
        # 固定大小的最新值摘要在预算裁剪后仍保留,无需为每根K线增加六列。
        "ema_latest": {f"ema{n}": _r(((pl.get("indicators") or {}).get("ema", {}).get(f"ema{n}") or [None])[-1])
                       for n in EMA_PERIODS},
        "evidence": [_ev_view(e, full) for e in evidence],
        "key_levels": [{"id": k["id"], "name": k["name"], "price": k["price"]} for k in item["key_levels"]],
        "local_decision": {k: dec.get(k) for k in keep if dec.get(k) is not None},
    }
    if full and pl.get("signals"):
        block["indicator_state"] = pl["signals"]
    block["bars_sent"] = len(block["bars"])
    return block


def build_model_input(inst_id: str, evidence: Dict, batch, bars_per_level: Dict[str, int],
                      max_chars: int, community_brief: Optional[Dict] = None):
    as_of = evidence["analysis_as_of"]
    trim: List[str] = []
    full = True
    scale = 1.0

    def assemble():
        levels = {}
        for lv in LEVELS:
            n = max(MIN_BARS_IN_PROMPT[lv], int(bars_per_level[lv] * scale))
            levels[lv] = _level_block(lv, evidence["levels"][lv], n, full, as_of)
        forming = {}
        for lv in LEVELS:
            f = batch.levels[lv].forming
            forming[lv] = ({"open_time": fmt_ts(f["ts"]), "o": f["o"], "h": f["h"], "l": f["l"],
                            "c": f["c"], "closed": False} if f else None)
        missing = [f"{lv}: {', '.join(evidence['levels'][lv]['quality'].get('issues') or [])}"
                   for lv in LEVELS if evidence["levels"][lv]["quality"].get("status") != "ok"]
        return {
            "inst_id": inst_id,
            "analysis_as_of": fmt_ts(as_of) + " (UTC+8)",
            "live_price": evidence["live_price"],
            "columns": COLUMNS,
            "units": {"time": "K线开盘时间 UTC+8", "vol": "合约张数(SWAP,不可跨币种比较)",
                      "vol_ccy": "基础币数量",
                      "ema_latest": "最后已收盘K线的EMA(span=n,adjust=False,min_periods=n);样本不足为null;不是双倍SMA"},
            "levels": levels,
            "forming_bars_unconfirmed": forming,
            "missing_or_degraded": missing,
            "community_brief": community_brief,
        }

    data = assemble()
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) > max_chars:
        full = False
        trim.append("删除指标状态、背离明细、旧笔和本地决策文案")
        data = assemble()
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    while len(text) > max_chars and scale > 0.2:
        scale = round(scale - 0.15, 2)
        data = assemble()
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        trim.append(f"K线历史缩短至 {int(scale * 100)}%")
    meta = {
        "chars": len(text),
        "est_tokens": int(len(text) / 2.5),
        "trim": trim,
        "over_budget": len(text) > max_chars,
        "bars_sent": {lv: data["levels"][lv]["bars_sent"] for lv in LEVELS},
        "last_closed_at": {lv: evidence["levels"][lv]["quality"].get("last_closed_at") for lv in LEVELS},
        "confirmed_count": {lv: evidence["levels"][lv]["quality"].get("confirmed_count") for lv in LEVELS},
        "quality": {lv: evidence["levels"][lv]["quality"].get("status") for lv in LEVELS},
    }
    return data, text, meta
