"""本地复核:数据质量、证据引用、信号锁定与时效、方向冲突、价格关系、净盈亏比。

模型结论只会被降级,不会被升级:模型说 watch,本地不改成 long/short。
"""

from __future__ import annotations

from typing import Dict, List, Optional

STRATEGY_VERSION = "rt-right-side-v1"
CONFIRM_TYPES = {"long": {"B2", "B3"}, "short": {"S2", "S3"}}
HIGHER_LEVELS = ("4H", "1D")
BAD_QUALITY = {"stale", "empty", "error", "insufficient"}


def _side_of(verdict: Optional[str], signal_type: Optional[str]) -> Optional[str]:
    if verdict in ("long", "short"):
        return verdict
    if signal_type:
        return "long" if signal_type[0] == "B" else "short"
    return None


def data_quality(evidence: Dict, required_levels: List[str]) -> Dict:
    bad, warn = [], []
    for lv, item in evidence["levels"].items():
        q = item["quality"]
        st = q.get("status")
        msg = f"{lv}: {st}" + (f"({'; '.join(q.get('issues') or [])})" if q.get("issues") else "")
        if st in BAD_QUALITY:
            (bad if lv in required_levels else warn).append(msg)
        elif q.get("issues"):
            warn.append(msg)
    return {"status": "insufficient" if bad else "ok", "blocking": bad, "warnings": warn}


def check_signal(e: Dict, side: str, as_of: int, level: Optional[str]) -> List[str]:
    """返回不满足"右侧已确认"的原因;空列表表示通过。"""
    probs = []
    if e.get("kind") != "bsp":
        return ["引用的证据不是买卖点"]
    if e.get("type") not in CONFIRM_TYPES[side]:
        probs.append(f"{e.get('type')} 不是{'B2/B3' if side == 'long' else 'S2/S3'}右侧确认点")
    if level and e.get("level") != level:
        probs.append(f"信号级别 {e.get('level')} 与声明的 {level} 不一致")
    if not e.get("locked") or not e.get("locked_at"):
        probs.append("信号未锁定")
    elif e["locked_at"] > as_of:
        probs.append("信号锁定时间晚于分析时刻(未来信息)")
    if e.get("known_at") is None or e["known_at"] > as_of:
        probs.append("信号在分析时刻尚不可知")
    if not e.get("active"):
        probs.append("信号已被更新的结构替代(不在活跃窗口)")
    if e.get("invalidated"):
        probs.append(f"信号已失效:{e['invalidated']}")
    return probs


def _resolve(ref: Dict, key_levels: Dict, tol: float, name: str, errs: List[str]) -> Optional[float]:
    kl = key_levels.get(ref["basis"])
    if not kl or kl.get("price") is None:
        errs.append(f"{name} 引用了不存在的关键位 {ref['basis']}")
        return None
    expect = kl["price"] * (1 + ref.get("offset_pct", 0) / 100)
    if abs(ref["price"] - expect) / expect > tol:
        errs.append(f"{name} 价格 {ref['price']} 与引用 {ref['basis']}={kl['price']}"
                    f"(偏移 {ref.get('offset_pct', 0)}%)不符")
        return None
    return expect


def net_rr(side: str, entry: float, stop: float, target: float, fee: float, slip: float) -> Dict:
    per_side = fee + slip
    if side == "long":
        reward, risk = target - entry, entry - stop
    else:
        reward, risk = entry - target, stop - entry
    cost_win = entry * per_side + target * per_side
    cost_loss = entry * per_side + stop * per_side
    nr, nk = reward - cost_win, risk + cost_loss
    return {"gross_rr": round(reward / risk, 3) if risk > 0 else None,
            "net_rr": round(nr / nk, 3) if nk > 0 and reward > 0 else None,
            "reward": reward, "risk": risk, "round_trip_cost_pct": round(per_side * 2 * 100, 4)}


def review_technical(model_status: str, model_out: Optional[Dict], schema_errors: List[str],
                     evidence: Dict, settings, model_error: Optional[str] = None) -> Dict:
    as_of = evidence["analysis_as_of"]
    live = evidence["live_price"]
    dq = data_quality(evidence, settings.required_levels)
    res: Dict = {"verdict": "watch", "right_side": "unknown", "structure_confirmed": False,
                 "price_ok": None, "rr": {"status": "not_checked"}, "signal": None,
                 "conflicts": [], "reasons": [], "downgrades": [], "data_quality": dq,
                 "model_status": model_status, "strategy_version": STRATEGY_VERSION}
    dirs = {lv: (evidence["levels"][lv].get("direction") or {}).get("dir") for lv in evidence["levels"]}
    res["directions"] = dirs

    if dq["status"] != "ok":
        res.update(verdict="insufficient_data")
        res["reasons"].append("必需周期数据不足或过期:" + ";".join(dq["blocking"]))
        return res
    if model_status != "ok" or model_out is None:
        res.update(verdict="unavailable")
        res["reasons"].append(f"模型结论不可用({model_status}{':' + model_error if model_error else ''}),无有效技术结论")
        return res
    if schema_errors:
        res.update(verdict="unavailable")
        res["reasons"].append("模型输出未通过 Schema 校验:" + ";".join(schema_errors[:8]))
        return res

    m = model_out
    res["right_side"] = m["right_side"]
    unknown_ids = [i for i in m["evidence_ids"] if i not in evidence["index"]]
    if unknown_ids:
        res.update(verdict="watch", right_side="unknown")
        res["downgrades"].append(f"模型引用了不存在的证据编号 {unknown_ids[:5]},视为编造证据")
        res["reasons"].append("模型证据无法核对 → 观望")
        return res

    side = _side_of(m["verdict"], m["signal_type"])
    if m["right_side"] == "confirmed":
        if not side:
            res["right_side"] = "pending"
            res["downgrades"].append("声称已确认但未给出方向/信号类型")
        else:
            cands = [evidence["index"][i] for i in m["evidence_ids"]
                     if evidence["index"][i].get("kind") == "bsp"]
            problems: List[str] = []
            passed = None
            for e in cands:
                p = check_signal(e, side, as_of, m["signal_level"])
                if not p:
                    passed = e
                    break
                problems.append(f"{e['id']}: " + "、".join(p))
            if passed:
                res["structure_confirmed"] = True
                res["signal"] = {k: passed.get(k) for k in ("id", "level", "type", "price",
                                                             "known_at", "locked_at", "score")}
            else:
                res["right_side"] = "pending"
                res["downgrades"].append("右侧确认未通过本地校验:" +
                                         (";".join(problems) if problems else "未引用任何买卖点证据"))
    if side:
        opp = "short" if side == "long" else "long"
        for lv in HIGHER_LEVELS:
            if dirs.get(lv) == opp:
                res["conflicts"].append(f"{lv} 结构方向为 {opp},与 {side} 冲突")

    want_trade = m["verdict"] in ("long", "short")
    if res["structure_confirmed"] and want_trade:
        perrs: List[str] = []
        kl, tol = evidence["key_levels"], settings.price_ref_tolerance
        if not m["entry"] or not m["stop"] or not m["targets"]:
            perrs.append("缺少入场/止损/目标价,无法核算盈亏比")
        else:
            entry = _resolve(m["entry"], kl, tol, "entry", perrs)
            stop = _resolve(m["stop"], kl, tol, "stop", perrs)
            t1 = _resolve(m["targets"][0], kl, tol, "targets[0]", perrs)
            if None not in (entry, stop, t1):
                ok_order = (stop < entry < t1) if side == "long" else (t1 < entry < stop)
                if not ok_order:
                    perrs.append(f"价格关系错误:{side} 要求 "
                                 f"{'止损<入场<目标' if side == 'long' else '目标<入场<止损'}"
                                 f"(止损 {stop},入场 {entry},目标 {t1})")
                elif live is None:
                    perrs.append("缺少现价")
                elif (side == "long" and live <= stop) or (side == "short" and live >= stop):
                    perrs.append(f"现价 {live} 已越过止损 {stop}")
                else:
                    planned = net_rr(side, entry, stop, t1, settings.fee_rate, settings.slippage_rate)
                    at_live = net_rr(side, live, stop, t1, settings.fee_rate, settings.slippage_rate)
                    passed = (at_live["net_rr"] or 0) >= settings.min_net_rr
                    res["rr"] = {"status": "passed" if passed else "failed",
                                 "min_net_rr": settings.min_net_rr,
                                 "at_live": at_live, "at_planned_entry": planned,
                                 "entry": entry, "stop": stop, "target1": t1, "live": live,
                                 "fee_rate": settings.fee_rate, "slippage_rate": settings.slippage_rate}
                    res["price_ok"] = passed
                    if not passed:
                        res["downgrades"].append(f"按现价 {live} 计算净盈亏比 {at_live['net_rr']} "
                                                 f"< {settings.min_net_rr},现价不适合参与")
        if perrs:
            res["rr"] = {"status": "not_checked", "errors": perrs}
            res["price_ok"] = False
            res["downgrades"].extend(perrs)

    if want_trade and res["structure_confirmed"] and res["price_ok"] and not res["conflicts"]:
        res["verdict"] = m["verdict"]
    else:
        res["verdict"] = "watch"
        if want_trade:
            if res["conflicts"]:
                res["downgrades"].append("大级别方向冲突 → 观望")
            res["reasons"].append(f"模型建议 {m['verdict']},本地复核未全部通过 → 观望")
    res["reasons"].extend(m["reasons"])
    return res
