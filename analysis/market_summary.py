"""供详细报告使用的已收盘行情摘要,不改变交易或结构判定。

EMA 采用工程展示参数 5/10/20/40/60/80,并不代表课程双倍 SMA。
仅归纳已计算指标与成交量,不从 OHLCV 推测订单流、持仓量或链上资金。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from analysis.chanlun.indicators import EMA_PERIODS
from analysis.validate import check_signal
from market.timeframes import DURATION_MS

REPORT_LEVELS = ("1W", "1D", "4H", "1H")
_DIRECTIONS = {"up": "向上", "down": "向下", "sideways": "震荡",
               "mixed": "方向分歧", "unknown": "方向未明"}


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _fmt(value) -> str:
    value = _number(value)
    return f"{value:.8g}" if value is not None else "未就绪"


def _at(values, index: Optional[int]) -> Optional[float]:
    if index is None or not isinstance(values, list) or not 0 <= index < len(values):
        return None
    return _number(values[index])


def _change(name: str, current: Optional[float], previous: Optional[float]) -> str:
    if current is None or previous is None:
        return f"{name}缺少前值,无法比较增减"
    word = "上升" if current > previous else "下降" if current < previous else "持平"
    return f"{name}较前一已收盘值{word}"


def _macd_interpretation(current: Dict, previous: Dict, count: int) -> str:
    dif, dea, hist = (current.get(k) for k in ("dif", "dea", "hist"))
    if any(v is None for v in (dif, dea, hist)):
        return "已收盘指标不足,无法判断零轴位置与增减"
    if dif > 0 and dea > 0:
        zero = "DIF 与 DEA 均在零轴上方"
    elif dif < 0 and dea < 0:
        zero = "DIF 与 DEA 均在零轴下方"
    elif dif == 0 and dea == 0:
        zero = "DIF 与 DEA 均为零"
    else:
        zero = "DIF 与 DEA 分处零轴两侧或有一项为零"
    old = previous.get("hist")
    if old is None:
        bars = "HIST 缺少前值,无法比较柱体变化"
    elif hist * old < 0:
        bars = "HIST 由负转正" if hist > 0 else "HIST 由正转负"
    elif hist == 0:
        bars = "HIST 为零"
    else:
        sign = "正柱" if hist > 0 else "负柱"
        change = "扩大" if abs(hist) > abs(old) else "缩短" if abs(hist) < abs(old) else "持平"
        bars = f"HIST {sign}{change}"
    notes = [zero, _change("DIF", dif, previous.get("dif")),
             _change("DEA", dea, previous.get("dea")), bars,
             "柱体变化只描述动能,不能单独确认右侧信号"]
    if count < 20:
        notes.insert(0, f"仅 {count} 根已收盘K线,少于慢线周期 20,预热不足")
    return ";".join(notes)


def summarize_levels(evidence: Dict) -> List[Dict]:
    """四周期固定顺序的可存储摘要;所有指标取已收盘K线位置,不使用 live_price。"""
    summaries = []
    for level in REPORT_LEVELS:
        item = (evidence.get("levels") or {}).get(level) or {}
        quality = item.get("quality") or {}
        payload = item.get("payload") or {}
        candles = payload.get("candles") or []
        # 正常 evidence 的 payload 已只含 confirm=1;这里再守住展示边界。
        positions = [i for i, candle in enumerate(candles) if candle.get("confirm") == 1]
        closed = [candles[i] for i in positions]
        count = len(closed)
        index = positions[-1] if positions else None
        previous_index = positions[-2] if len(positions) > 1 else None
        ind = payload.get("indicators") or {}
        last = closed[-1] if closed else {}
        close = _number(last.get("c"))
        direction = item.get("direction") or {}
        metrics = [{"name": "数据状态", "value": f"{count} 根已收盘K线",
                    "interpretation": ";".join(quality.get("issues") or []) or
                    ("指标取最后已收盘K线,未收盘价格不参与" if count else "没有已收盘行情,指标不可用")}]

        ema = {n: _at((ind.get("ema") or {}).get(f"ema{n}"), index) for n in EMA_PERIODS}
        metrics.append({"name": "EMA 指数均线", "value": " / ".join(f"EMA{n}={_fmt(ema[n])}" for n in EMA_PERIODS),
                        "interpretation": "按该周期已收盘的收盘价计算;EMA80 至少需要 80 根,其余均线也需达到对应根数"})
        if any(ema[n] is None for n in EMA_PERIODS):
            missing = "/".join(f"EMA{n}" for n in EMA_PERIODS if ema[n] is None)
            arrangement = "尚不能判断完整排列"
            ema_note = f"{missing}未就绪;当前 {count} 根已收盘K线,完整排列至少需要 80 根"
        else:
            pairs = list(zip(EMA_PERIODS, EMA_PERIODS[1:]))
            if all(ema[a] > ema[b] for a, b in pairs):
                arrangement = "多头排列:EMA5 > EMA10 > EMA20 > EMA40 > EMA60 > EMA80"
            elif all(ema[a] < ema[b] for a, b in pairs):
                arrangement = "空头排列:EMA5 < EMA10 < EMA20 < EMA40 < EMA60 < EMA80"
            else:
                arrangement = "均线交错或相等,没有完整单向排列"
            ema_note = "排列描述趋势背景,不等于缠论买卖点确认"
        metrics.append({"name": "EMA 排列", "value": arrangement, "interpretation": ema_note})

        macd = ind.get("macd") or {}
        current = {k: _at(macd.get(k), index) for k in ("dif", "dea", "hist")}
        previous = {k: _at(macd.get(k), previous_index) for k in current}
        metrics.append({"name": "MACD(10,20,5)",
                        "value": " / ".join(f"{k.upper()}={_fmt(v)}" for k, v in current.items()),
                        "interpretation": _macd_interpretation(current, previous, count)})

        kdj = {k: _at((ind.get("kdj") or {}).get(k), index) for k in ("k", "d", "j")}
        if kdj["k"] is None:
            kdj_note = "没有可用的已收盘KDJ指标"
        else:
            kdj_note = "K 高于 80" if kdj["k"] > 80 else "K 低于 20" if kdj["k"] < 20 else "K 处于 20–80 区间"
            kdj_note += ";" + _change("J", kdj["j"], _at((ind.get("kdj") or {}).get("j"), previous_index))
            if count < 9:
                kdj_note = f"仅 {count} 根已收盘K线,不足 9 根窗口;" + kdj_note
            kdj_note += ";超买超卖状态不单独确认反转"
        metrics.append({"name": "KDJ(9,3,3)", "value": " / ".join(f"{k.upper()}={_fmt(v)}" for k, v in kdj.items()),
                        "interpretation": kdj_note})

        rsi = {n: _at((ind.get("rsi") or {}).get(f"rsi{n}"), index) for n in (6, 12, 24)}
        rsi_note = ("RSI6 高于 70" if rsi[6] > 70 else "RSI6 低于 30" if rsi[6] < 30 else "RSI6 处于 30–70 区间") if rsi[6] is not None else "没有可用的已收盘RSI指标"
        if 0 < count < 24:
            rsi_note = f"仅 {count} 根已收盘K线,不足最长周期 24,预热不足;" + rsi_note
        metrics.append({"name": "RSI(6,12,24)", "value": " / ".join(f"RSI{n}={_fmt(v)}" for n, v in rsi.items()),
                        "interpretation": rsi_note})

        vol = _number(last.get("vol"))
        if count < 20:
            volume_note = f"{count} 根已收盘K线,不足 20 根,不计算量比"
        else:
            volumes = [_number(c.get("vol")) for c in closed[-20:]]
            mean = sum(volumes) / 20 if all(v is not None for v in volumes) else None
            if mean is None or mean <= 0 or vol is None:
                volume_note = "20 根均量缺失或为零,无法计算量比"
            else:
                ratio = vol / mean
                state = "放量" if ratio > 1.8 else "缩量" if ratio < 0.6 else "正常量能"
                volume_note = f"相对最近20根均量(含本根)为 {ratio:.2f} 倍,{state};仅为成交量,不代表资金净流入"
        metrics.append({"name": "成交量", "value": f"{_fmt(vol)} 合约张数" if vol is not None else "未就绪",
                        "interpretation": volume_note})

        active_bsp_levels = {
            "L-" + e["id"][2:] for e in item.get("evidence", [])
            if e.get("kind") == "bsp" and e.get("type") in ("B2", "B3", "S2", "S3")
            and not check_signal(e, "long" if e["type"].startswith("B") else "short",
                                 evidence.get("analysis_as_of", 0), level)
        }
        summaries.append({
            "level": level, "quality": str(quality.get("status") or ("ok" if count else "empty")),
            "last_closed_at": last["ts"] + DURATION_MS[level] if last.get("ts") is not None else None,
            "close": close,
            "direction": direction.get("desc") or _DIRECTIONS.get(direction.get("dir"), "方向未明"),
            "metrics": metrics,
            "key_levels": [{"id": k["id"], "name": k["name"], "price": k["price"]}
                           for k in item.get("key_levels", [])
                           if k.get("id") and k.get("name") and _number(k.get("price")) is not None
                           and ("-BSP-" not in k["id"] or k["id"] in active_bsp_levels)],
        })
    return summaries
