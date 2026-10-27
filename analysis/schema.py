"""模型输出的严格 Schema 校验。返回 (规范化结果, 错误列表);有错误即视为模型输出无效。"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

VERDICTS = {"long", "short", "watch"}
RIGHT_SIDE = {"confirmed", "pending", "none", "unknown"}
LEVELS = {"1H", "4H", "1D", "1W"}
SIGNAL_TYPES = {"B1", "B2", "B3", "S1", "S2", "S3"}
SENTIMENTS = {"bullish", "bearish", "mixed", "neutral", "unknown"}
DIRECTIONS = {"up", "down", "sideways", "mixed", "unknown"}
STANCES = {"support", "oppose", "neutral"}
KINDS = {"opinion", "rumor", "verifiable_event"}


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) and v > 0 else None


def _str_list(d: Dict, key: str, errs: List[str], required=False) -> List[str]:
    v = d.get(key)
    if v is None:
        if required:
            errs.append(f"缺少字段 {key}")
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errs.append(f"{key} 必须是字符串数组")
        return []
    return v


def _price_ref(v, key: str, errs: List[str]) -> Optional[Dict]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        errs.append(f"{key} 缺少 basis(必须引用关键位 id)")
        return None
    if not isinstance(v, dict):
        errs.append(f"{key} 格式错误")
        return None
    price = _num(v.get("price"))
    if price is None:
        errs.append(f"{key}.price 不是正数")
        return None
    basis = v.get("basis")
    if not isinstance(basis, str) or not basis:
        errs.append(f"{key}.basis 缺失")
        return None
    off = v.get("offset_pct", 0) or 0
    if isinstance(off, bool) or not isinstance(off, (int, float)) or not math.isfinite(off) or abs(off) > 20:
        errs.append(f"{key}.offset_pct 无效")
        return None
    return {"price": price, "basis": basis, "offset_pct": float(off)}


def validate_tech(raw: Dict) -> Tuple[Dict, List[str]]:
    errs: List[str] = []
    if not isinstance(raw, dict):
        return {}, ["输出不是 JSON 对象"]
    out: Dict = {}
    v = raw.get("verdict")
    if v not in VERDICTS:
        errs.append(f"verdict 枚举无效: {v!r}")
    out["verdict"] = v
    rs = raw.get("right_side")
    if rs not in RIGHT_SIDE:
        errs.append(f"right_side 枚举无效: {rs!r}")
    out["right_side"] = rs
    sl = raw.get("signal_level")
    if sl is not None and sl not in LEVELS:
        errs.append(f"signal_level 无效: {sl!r}")
    out["signal_level"] = sl
    st = raw.get("signal_type")
    if st is not None and st not in SIGNAL_TYPES:
        errs.append(f"signal_type 无效: {st!r}")
    out["signal_type"] = st
    out["evidence_ids"] = _str_list(raw, "evidence_ids", errs, required=True)
    lv = raw.get("levels")
    if not isinstance(lv, dict) or not all(isinstance(lv.get(k), str) for k in LEVELS):
        errs.append("levels 必须包含 1W/1D/4H/1H 四个字符串")
        lv = {k: (lv or {}).get(k) if isinstance(lv, dict) else None for k in LEVELS}
    out["levels"] = {k: lv.get(k) for k in ("1W", "1D", "4H", "1H")}
    out["entry"] = _price_ref(raw.get("entry"), "entry", errs)
    out["stop"] = _price_ref(raw.get("stop"), "stop", errs)
    tg = raw.get("targets", [])
    if not isinstance(tg, list):
        errs.append("targets 必须是数组")
        tg = []
    out["targets"] = [t for t in (_price_ref(x, f"targets[{i}]", errs) for i, x in enumerate(tg)) if t]
    conf = raw.get("confidence")
    if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 100):
        errs.append("confidence 必须是 0-100")
        conf = None
    out["confidence"] = conf
    out["reasons"] = _str_list(raw, "reasons", errs, required=True)
    out["risks"] = _str_list(raw, "risks", errs)
    for k in ("invalidation", "watch_next"):
        val = raw.get(k)
        if val is not None and not isinstance(val, str):
            errs.append(f"{k} 必须是字符串")
            val = None
        out[k] = val
    if v in ("long", "short") and rs != "confirmed":
        errs.append("verdict 为 long/short 时 right_side 必须为 confirmed")
    if st and v in ("long", "short") and ((v == "long") != (st[0] == "B")):
        errs.append(f"signal_type {st} 与 verdict {v} 方向不一致")
    return out, errs


def validate_community(raw: Dict, allowed_symbols: List[str]) -> Tuple[Dict, List[str]]:
    errs: List[str] = []
    if not isinstance(raw, dict):
        return {}, ["输出不是 JSON 对象"]
    ov = raw.get("overall") or {}
    if not isinstance(ov, dict) or ov.get("sentiment") not in SENTIMENTS:
        errs.append("overall.sentiment 无效")
        ov = {"sentiment": "unknown"} if not isinstance(ov, dict) else ov
    overall = {"sentiment": ov.get("sentiment"), "summary": ov.get("summary") if isinstance(ov.get("summary"), str) else None,
               "themes": [t for t in (ov.get("themes") or []) if isinstance(t, str)]}
    assets = []
    items = raw.get("assets")
    if not isinstance(items, list):
        errs.append("assets 必须是数组")
        items = []
    for a in items:
        if not isinstance(a, dict):
            errs.append("assets 元素不是对象")
            continue
        sym = a.get("symbol")
        if sym not in allowed_symbols:
            errs.append(f"未请求的币种 {sym!r}")
            continue
        aerr: List[str] = []
        if a.get("sentiment") not in SENTIMENTS:
            aerr.append(f"{sym}.sentiment 无效")
        te = a.get("trend_expectation") or {}
        if not isinstance(te, dict) or te.get("direction") not in DIRECTIONS:
            aerr.append(f"{sym}.trend_expectation.direction 无效")
            te = {"direction": "unknown"}
        evs = []
        for e in a.get("evidence") or []:
            if not isinstance(e, dict) or not isinstance(e.get("ref"), str):
                aerr.append(f"{sym}.evidence 元素无效")
                continue
            if e.get("stance") not in STANCES or e.get("kind") not in KINDS:
                aerr.append(f"{sym}.evidence {e.get('ref')} 的 stance/kind 无效")
                continue
            evs.append({"ref": e["ref"], "stance": e["stance"], "kind": e["kind"],
                        "summary": e.get("summary") if isinstance(e.get("summary"), str) else ""})
        errs.extend(aerr)
        assets.append({"symbol": sym, "sentiment": a.get("sentiment"),
                       "trend_expectation": {"direction": te.get("direction"),
                                             "horizon": te.get("horizon") if isinstance(te.get("horizon"), str) else None},
                       "themes": [t for t in (a.get("themes") or []) if isinstance(t, str)],
                       "counterarguments": [t for t in (a.get("counterarguments") or []) if isinstance(t, str)],
                       "risks": [t for t in (a.get("risks") or []) if isinstance(t, str)],
                       "evidence": evs})
    return {"overall": overall, "assets": assets}, errs
