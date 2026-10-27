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


def valid_enum(value, choices) -> bool:
    """先检查类型,避免模型输出数组/对象时集合查找抛出 TypeError。"""
    return isinstance(value, str) and value in choices


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _num(v) -> Optional[float]:
    return float(v) if _finite_number(v) and v > 0 else None


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


def _optional_str(d: Dict, key: str, errs: List[str]):
    value = d.get(key)
    if value is not None and not isinstance(value, str):
        errs.append(f"{key} 必须是字符串")
        return None
    return value


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
    off = v.get("offset_pct", 0)
    if not _finite_number(off) or abs(off) > 20:
        errs.append(f"{key}.offset_pct 无效")
        return None
    return {"price": price, "basis": basis, "offset_pct": float(off)}


def validate_tech(raw: Dict) -> Tuple[Dict, List[str]]:
    errs: List[str] = []
    if not isinstance(raw, dict):
        return {}, ["输出不是 JSON 对象"]
    out: Dict = {}
    v = raw.get("verdict")
    if not valid_enum(v, VERDICTS):
        errs.append(f"verdict 枚举无效: {v!r}")
        v = None
    out["verdict"] = v
    rs = raw.get("right_side")
    if not valid_enum(rs, RIGHT_SIDE):
        errs.append(f"right_side 枚举无效: {rs!r}")
        rs = None
    out["right_side"] = rs
    sl = raw.get("signal_level")
    if sl is not None and not valid_enum(sl, LEVELS):
        errs.append(f"signal_level 无效: {sl!r}")
        sl = None
    out["signal_level"] = sl
    st = raw.get("signal_type")
    if st is not None and not valid_enum(st, SIGNAL_TYPES):
        errs.append(f"signal_type 无效: {st!r}")
        st = None
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
    if conf is not None and (not _finite_number(conf) or not 0 <= conf <= 100):
        errs.append("confidence 必须是 0-100")
        conf = None
    out["confidence"] = conf
    out["reasons"] = _str_list(raw, "reasons", errs, required=True)
    out["risks"] = _str_list(raw, "risks", errs)
    for k in ("invalidation", "watch_next"):
        out[k] = _optional_str(raw, k, errs)
    if v in ("long", "short") and rs != "confirmed":
        errs.append("verdict 为 long/short 时 right_side 必须为 confirmed")
    if st and v in ("long", "short") and ((v == "long") != (st[0] == "B")):
        errs.append(f"signal_type {st} 与 verdict {v} 方向不一致")
    return out, errs


def validate_community(raw: Dict, allowed_symbols: List[str]) -> Tuple[Dict, List[str]]:
    errs: List[str] = []
    if not isinstance(raw, dict):
        return {}, ["输出不是 JSON 对象"]
    ov = raw.get("overall")
    overall = None
    if not isinstance(ov, dict):
        errs.append("overall 必须是对象")
    else:
        oerr: List[str] = []
        if not valid_enum(ov.get("sentiment"), SENTIMENTS):
            oerr.append("sentiment 无效")
        summary = _optional_str(ov, "summary", oerr)
        themes = _str_list(ov, "themes", oerr)
        errs.extend(f"overall.{e}" for e in oerr)
        if not oerr:
            overall = {"sentiment": ov["sentiment"], "summary": summary, "themes": themes}
    assets: Dict[str, Dict] = {}
    asset_errors: Dict[str, List[str]] = {}
    seen = set()
    items = raw.get("assets")
    if not isinstance(items, list):
        errs.append("assets 必须是数组")
        items = []
    for a in items:
        if not isinstance(a, dict):
            errs.append("assets 元素不是对象")
            continue
        sym = a.get("symbol")
        if not isinstance(sym, str) or sym not in allowed_symbols:
            errs.append(f"未请求的币种 {sym!r}")
            continue
        if sym in seen:
            error = f"{sym}: 重复的币种结论"
            errs.append(error)
            asset_errors.setdefault(sym, []).append(error)
            assets.pop(sym, None)
            continue
        seen.add(sym)
        aerr: List[str] = []
        if not valid_enum(a.get("sentiment"), SENTIMENTS):
            aerr.append("sentiment 无效")
        te = a.get("trend_expectation")
        if not isinstance(te, dict) or not valid_enum(te.get("direction"), DIRECTIONS):
            aerr.append("trend_expectation.direction 无效")
            te = {"direction": "unknown"}
        horizon = _optional_str(te, "horizon", aerr)
        evs = []
        evidence = a.get("evidence", [])
        if not isinstance(evidence, list):
            aerr.append("evidence 必须是数组")
            evidence = []
        for e in evidence:
            if not isinstance(e, dict) or not isinstance(e.get("ref"), str) or not e["ref"]:
                aerr.append("evidence 元素无效")
                continue
            if not valid_enum(e.get("stance"), STANCES) or not valid_enum(e.get("kind"), KINDS):
                aerr.append(f"evidence {e.get('ref')} 的 stance/kind 无效")
                continue
            evs.append({"ref": e["ref"], "stance": e["stance"], "kind": e["kind"],
                        "summary": _optional_str(e, "summary", aerr) or ""})
        normalized = {"symbol": sym, "sentiment": a.get("sentiment"),
                      "trend_expectation": {"direction": te.get("direction"), "horizon": horizon},
                      "themes": _str_list(a, "themes", aerr),
                      "counterarguments": _str_list(a, "counterarguments", aerr),
                      "risks": _str_list(a, "risks", aerr), "evidence": evs}
        if aerr:
            asset_errors[sym] = [f"{sym}.{e}" for e in aerr]
            errs.extend(asset_errors[sym])
        else:
            assets[sym] = normalized
    for sym in allowed_symbols:
        if sym not in seen:
            asset_errors[sym] = [f"{sym}: 模型未返回该币种结论"]
            errs.extend(asset_errors[sym])
    return {"overall": overall, "assets": list(assets.values()), "asset_errors": asset_errors}, errs
