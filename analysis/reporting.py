"""详细中文报告：保存本批次事实与条件预案，不重新判定或升级交易结论。"""

from __future__ import annotations

from typing import Dict, List, Optional

from analysis.market_summary import summarize_levels
from analysis.validate import check_signal

REPORT_VERSION = "report-observation-v1"
FOCUS_LEVEL = "4H"


def _price(value) -> str:
    return "未知" if value is None else f"{value:.8g}"


def _label(point: Dict) -> str:
    return f"{point['name']}（{_price(point['price'])}）"


def _boundaries(evidence: Dict):
    """收盘价上下最近观察边界；不把未确认买卖点拿来构造预案。"""
    item = evidence["levels"][FOCUS_LEVEL]
    cs = item.get("payload", {}).get("candles", [])
    close = cs[-1]["c"] if cs else None
    allowed = {f"L-{FOCUS_LEVEL}-{x}" for x in
               ("EMA10", "EMA20", "EMA40", "ZG", "ZD", "RECENT_HIGH", "RECENT_LOW")}
    points = [p for p in item.get("key_levels", [])
              if p.get("id") in allowed and isinstance(p.get("price"), (int, float)) and p["price"] > 0]
    if close is None:
        return None, None
    below = [p for p in points if p["price"] < close]
    above = [p for p in points if p["price"] > close]
    return (max(below, key=lambda p: p["price"]) if below else None,
            min(above, key=lambda p: p["price"]) if above else None)


def _scenarios(evidence: Dict, tech: Dict, settings) -> List[Dict]:
    support, resistance = _boundaries(evidence)
    quality = evidence["levels"][FOCUS_LEVEL]["quality"]
    problems = list(tech["data_quality"].get("blocking", []))
    if quality.get("status") != "ok":
        problems.append(f"{FOCUS_LEVEL} 数据状态为 {quality.get('status')}")
    if quality.get("gaps"):
        problems.append(f"{FOCUS_LEVEL} 已收盘序列存在缺口")
    risk_check = (f"确认后以届时现价、本地可核对的止损与目标重新核算；"
                  f"双边成本后的净盈亏比须 ≥ {settings.min_net_rr:g}，否则继续等待。")
    result = []
    for key, title, point in (("up", "情景 A · 向上突破后回试", resistance),
                              ("down", "情景 B · 向下跌破后回抽", support),
                              ("range", "情景 C · 区间内继续等待", support if resistance else None)):
        row = {"id": key, "title": title, "status": "conditional", "level": FOCUS_LEVEL,
               "setup": "", "confirmation": [], "invalidation": [], "action": "", "level_ids": []}
        if problems or point is None:
            row.update(status="unavailable", setup="；".join(problems) if problems else "缺少可核对的观察边界。",
                       action="补齐有效行情与关键位后再评估，不根据缺失数据生成交易条件。")
        elif key == "range":
            row.update(
                setup=f"以 {_label(support)} 与 {_label(resistance)} 作为本批次的上下观察边界。",
                confirmation=["价格继续处于两条边界之间，且没有新的有效右侧确认时，保持等待。",
                              "对照 1H 的局部变化与 4H/1D 的方向；单次金叉或触线不等于反转。"],
                invalidation=["已收盘价格越过任一观察边界时，重新评估相应突破路径。"],
                action="不把本报告中的区间预案当成自动高抛低吸信号。",
                level_ids=[support["id"], resistance["id"]])
        else:
            up = key == "up"
            row.update(
                setup=f"观察 {_label(point)}。这是条件预案，尚未据此确认新的买卖点。",
                confirmation=[
                    f"4H 已收盘价格{'突破' if up else '跌破'}该边界，再等待{'回踩不跌回下方' if up else '回抽不重新站上'}；仅未收盘触及不足以确认。",
                    f"后续本地证据出现已锁定、仍活跃且未失效的 {'B2/B3' if up else 'S2/S3'}，可知时间不得晚于新报告的分析时刻。",
                    f"4H 与 1D 结构方向不得与{'做多' if up else '做空'}冲突。",
                    risk_check],
                invalidation=[f"已收盘价格重新{'跌回该边界下方' if up else '站上该边界'}，取消本次突破观察。",
                              "后续确认信号失效、上级方向冲突或数据过期，均需重新评估。"],
                action="等待新一批已收盘数据复核；边界不是自动入场价或止损价，均线位置也会随收盘更新。",
                level_ids=[point["id"]])
        result.append(row)
    return result


def _plan(tech: Dict, final: Dict, settings) -> Dict:
    tradable = final["final"] in ("long", "short")
    rr = tech.get("rr") or {}
    items = [f"当前执行判断：{final['label']}。"]
    if tradable:
        items.append("本地结构与现价条件已通过，仍需在实际决策时重新核对最新行情。")
    else:
        items.append("当前没有通过全部条件的参与结论；下列条件预案不构成立即入场信号。")
    signal = tech.get("signal")
    if signal:
        items.append(f"已核对信号：{signal['level']} {signal['type']}，证据编号 {signal['id']}。")
    if all(rr.get(k) is not None for k in ("entry", "stop", "target1")):
        prefix = "经复核的计划参考" if tradable else "技术参考（最终仍需等待）"
        items.append(f"{prefix}：入场 {_price(rr['entry'])}，止损 {_price(rr['stop'])}，第一目标 {_price(rr['target1'])}。")
        net = (rr.get("at_live") or {}).get("net_rr")
        items.append(f"按本批次现价 {_price(rr.get('live'))} 计算净盈亏比 {_price(net)}，门槛 {settings.min_net_rr:g}。")
    else:
        items.append("未形成可核对的入场、止损与目标组合，暂不生成数值交易计划。")
    items.append(f"成本假设：单边手续费 {settings.fee_rate * 100:g}%、滑点 {settings.slippage_rate * 100:g}%，计算包含入场及出场。")
    items.append("未取得账户规模与风险预算，本报告不计算仓位或杠杆。")
    return {"status": "技术候选" if tradable else "等待条件", "items": items}


def build_detailed_report(inst_id: str, evidence: Dict, tech: Dict, final: Dict,
                          community: Dict, model: Optional[Dict], settings) -> Dict:
    """输入全是同次分析的数据；结果可直接保存，不含原始 K 线或模型隐式推理。"""
    levels = summarize_levels(evidence)
    model_usable = (model is not None and tech["model_status"] == "ok"
                    and tech["verdict"] not in ("unavailable", "insufficient_data")
                    and not tech.get("downgrades") and not tech.get("conflicts")
                    and model.get("verdict") == tech["verdict"] == final["final"]
                    and all(i in evidence["index"] for i in model.get("evidence_ids", [])))
    for row in levels:
        lv = row["level"]
        signals = [e for e in evidence["levels"][lv].get("evidence", [])
                   if e.get("kind") == "bsp" and e.get("type") in ("B2", "B3", "S2", "S3")
                   and not check_signal(e, "long" if e["type"].startswith("B") else "short",
                                        evidence["analysis_as_of"], lv)]
        row["metrics"].append({
            "name": "本地右侧证据", "value": "；".join(f"{e['type']} · {e['id']}" for e in signals[-3:]) or "暂无有效的已锁定 B2/B3/S2/S3",
            "interpretation": "这是该周期的结构证据清单；参与与否还取决于方向、模型复核与现价风险收益。"})
        row["model_comment"] = model.get("levels", {}).get(lv) if model_usable else None

    if tech["verdict"] == "insufficient_data":
        summary = "必需周期的数据不足或过期，本次不作有效技术判断。下方仅保留数据现状供核对。"
    elif tech["verdict"] == "unavailable":
        summary = "当前无有效模型技术结论；本地指标和条件预案可用于观察，尚未形成可参与的交易判断。"
    else:
        structure = "右侧结构已通过复核" if tech.get("structure_confirmed") else "右侧结构尚未通过复核"
        price = "现价条件通过" if tech.get("price_ok") else "现价条件未通过或尚未核算"
        summary = f"{structure}，{price}。结合社区参考后，当前结论为：{final['label']}。"

    risk_items = list(tech["data_quality"].get("blocking", [])) + list(tech["data_quality"].get("warnings", []))
    risk_items += list(tech.get("conflicts", [])) + list(tech.get("downgrades", []))
    risk_items += list(tech.get("reasons", [])) if tech["verdict"] in ("unavailable", "insufficient_data") else []
    risk_items += list(final.get("notes", []))
    if not community.get("available"):
        risk_items.append(community.get("note") or "暂无有效社区参考。")
    risk_items += ["MACD/KDJ/RSI 与均线描述当前状态，单一指标变化不能直接确认结构反转。",
                   "价格与边界来自本批次快照；下一次收盘、突破失败或数据变化后需重新评估。"]
    funds = [
        {"name": "订单流 Delta / CVD", "status": "未接入", "note": "当前只有 K 线成交量，不能据此判断主动买卖差额。"},
        {"name": "持仓量 OI 与资金费率", "status": "未接入", "note": "本次输入没有这些数据，不判断增仓、减仓或拥挤程度。"},
        {"name": "交易所 / 链上资金净流入", "status": "未接入", "note": "未接入链上及资金流数据源，不判断资金吸筹或派发。"},
        {"name": "社区观点", "status": "可参考" if community.get("available") else "不可用",
         "note": "社区样本反映公开观点，不代表实际资金流或持仓。"},
    ]
    return {
        "version": REPORT_VERSION, "headline": f"{inst_id} · {final['label']}",
        "summary": summary, "focus_level": FOCUS_LEVEL, "as_of": evidence["analysis_as_of"],
        "basis": "以已收盘 K 线计算指标与结构，现价单独展示；默认展开 4H，结合 1D/1W 背景和 1H 证据。",
        "levels": levels, "scenarios": _scenarios(evidence, tech, settings),
        "plan": _plan(tech, final, settings), "funds": funds,
        "risks": list(dict.fromkeys(risk_items)),
        "next_watch": ["观察下一根 1H/4H 收盘，重新核对边界突破、回试与信号锁定状态。",
                       "检查 4H 与 1D 是否出现方向冲突，再以最新现价复核净盈亏比。",
                       "行情过期、信号失效或关键位改变时更新判断，不沿用本报告的旧价格。"],
        "conclusion": summary,
        "profile": {"id": REPORT_VERSION, "category": "engineering_observation", "signal_rules": tech["strategy_version"],
                    "ema_periods": [5, 10, 20, 40, 60, 80], "probability": None},
    }
