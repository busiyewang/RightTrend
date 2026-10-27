"""技术指标计算:MACD / KDJ / RSI。

公式与《缠论MACD币圈实战手册》及国内软件惯例一致:
- MACD(10,20,5),HIST = 2 × (DIF − DEA)
- KDJ(9,3,3),K/D 用 SMA(x,n,1) 平滑(即 alpha=1/n 的 EMA)
- RSI 用 Wilder 平滑(SMA(x,n,1))
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _sma_cn(series: pd.Series, n: int) -> pd.Series:
    """国内软件的 SMA(X, n, 1):Y = (X + (n-1)*Y') / n,等价 alpha=1/n 的 EMA。"""
    return series.ewm(alpha=1.0 / n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 10, slow: int = 20, signal: int = 5):
    """返回 (dif, dea, hist),hist = 2*(dif-dea)。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        n: int = 9, k_n: int = 3, d_n: int = 3):
    """返回 (k, d, j)。"""
    llv = low.rolling(n, min_periods=1).min()
    hhv = high.rolling(n, min_periods=1).max()
    rng = (hhv - llv).replace(0, np.nan)
    rsv = ((close - llv) / rng * 100).fillna(50.0)
    k = _sma_cn(rsv, k_n)
    d = _sma_cn(k, d_n)
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int) -> pd.Series:
    diff = close.diff()
    up = _sma_cn(diff.clip(lower=0).fillna(0), n)
    total = _sma_cn(diff.abs().fillna(0), n)
    out = up / total.replace(0, np.nan) * 100
    return out.fillna(50.0)


def compute_all(candles: list[dict]) -> dict:
    """candles: [{ts,o,h,l,c,vol,...}] 升序。返回各指标序列(list,NaN→None)。"""
    df = pd.DataFrame(candles)
    close, high, low = df["c"], df["h"], df["l"]
    dif, dea, hist = macd(close)
    k, d, j = kdj(high, low, close)

    def tolist(s: pd.Series):
        return [None if (v is None or (isinstance(v, float) and np.isnan(v))) else round(float(v), 6)
                for v in s.tolist()]

    return {
        "macd": {"dif": tolist(dif), "dea": tolist(dea), "hist": tolist(hist)},
        "kdj": {"k": tolist(k), "d": tolist(d), "j": tolist(j)},
        "rsi": {"rsi6": tolist(rsi(close, 6)), "rsi12": tolist(rsi(close, 12)),
                "rsi24": tolist(rsi(close, 24))},
    }


def analyze_signals(candles: list[dict], bis: list[dict] | None = None) -> dict:
    """量价/主力行为 + KDJ/RSI 状态与背离分析(供前端分析面板)。
    规则依据手册第 11-13 章:量比=量/MA(20),>1.8 放量、<0.6 缩量;
    量价八组合;低位吸筹/高位派发特征;RSI 常规背离=反转、隐藏背离=中继。
    candles 须为升序、仅含已收盘K线;bis 用于 RSI 背离(取笔端点比较)。"""
    if len(candles) < 25:
        return {}
    df = pd.DataFrame(candles)
    opn, high, low, close, vol = df["o"], df["h"], df["l"], df["c"], df["vol"]
    vol_ma20 = vol.rolling(20, min_periods=1).mean()

    # ---------- 量能与量价组合 ----------
    ratio = float(vol.iloc[-1] / vol_ma20.iloc[-1]) if vol_ma20.iloc[-1] > 0 else 1.0
    vol_state = "放量" if ratio > 1.8 else ("缩量" if ratio < 0.6 else "正常")
    p_up = bool(close.iloc[-1] >= close.iloc[-2])
    v_up = bool(vol.iloc[-1] >= vol.iloc[-2])
    combo, combo_note = {
        (True, True): ("价涨量增", "上涨健康,延续概率大"),
        (True, False): ("价涨量缩", "上涨乏力,高位警惕回调"),
        (False, True): ("价跌量增", "抛压沉重,低位或是恐慌出清"),
        (False, False): ("价跌量缩", "抛压衰竭,关注止跌信号"),
    }[(p_up, v_up)]

    # ---------- 主力行为(近 60 根已收盘K,仅基于量价) ----------
    w = min(60, len(df))
    hi = float(high.tail(w).max())
    lo = float(low.tail(w).min())
    pos = (float(close.iloc[-1]) - lo) / (hi - lo) if hi > lo else 0.5
    heavy_up = heavy_down = stall = lower_sh = upper_sh = quiet = 0
    for i in range(len(df) - w, len(df)):
        ma_ = float(vol_ma20.iloc[i])
        r = float(vol.iloc[i]) / ma_ if ma_ > 0 else 1.0
        o_, c_ = float(opn.iloc[i]), float(close.iloc[i])
        h_, l_ = float(high.iloc[i]), float(low.iloc[i])
        rng = h_ - l_
        if r < 0.6:
            quiet += 1
        if r > 1.8 and rng > 0:
            if c_ >= o_:
                heavy_up += 1
            else:
                heavy_down += 1
            if abs(c_ - o_) < 0.3 * rng:
                stall += 1
            if (min(o_, c_) - l_) > 0.6 * rng:
                lower_sh += 1
            if (h_ - max(o_, c_)) > 0.6 * rng:
                upper_sh += 1
    evidence = []
    concl = "无明显主力行为特征"
    if pos <= 0.35:
        if lower_sh >= 2:
            evidence.append(f"低位放量长下影 {lower_sh} 次(下方有承接)")
        if heavy_up >= 2:
            evidence.append(f"低位放量阳线 {heavy_up} 根")
        if quiet >= w * 0.4:
            evidence.append(f"低位缩量 {quiet}/{w} 根(浮筹沉淀)")
        if len(evidence) >= 2:
            concl = "疑似主力吸筹"
        elif len(evidence) == 1:
            concl = "或有吸筹迹象(待确认)"
    elif pos >= 0.65:
        if stall >= 2:
            evidence.append(f"高位放量滞涨 {stall} 次")
        if upper_sh >= 2:
            evidence.append(f"高位放量长上影 {upper_sh} 次(上方抛压)")
        if heavy_down >= 2:
            evidence.append(f"高位放量阴线 {heavy_down} 根")
        if len(evidence) >= 2:
            concl = "疑似主力派发"
        elif len(evidence) == 1:
            concl = "或有派发迹象(待确认)"
    else:
        evidence.append("价格处于区间中部,量价特征不典型")
    zone = "低位" if pos <= 0.35 else ("高位" if pos >= 0.65 else "中部")

    # ---------- KDJ 状态 ----------
    k, d, j = kdj(high, low, close)
    kv, dv, jv = float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1])
    kdj_states = []
    cross = None
    for i in range(max(1, len(df) - 3), len(df)):
        if k.iloc[i - 1] <= d.iloc[i - 1] and k.iloc[i] > d.iloc[i]:
            cross = "金叉"
        elif k.iloc[i - 1] >= d.iloc[i - 1] and k.iloc[i] < d.iloc[i]:
            cross = "死叉"
    if cross:
        kdj_states.append(f"近3根内{cross}")
    if kv > 80:
        kdj_states.append("超买区(K>80)")
    elif kv < 20:
        kdj_states.append("超卖区(K<20)")
    if jv > 100:
        kdj_states.append("J值高位钝化(>100)")
    elif jv < 0:
        kdj_states.append("J值低位钝化(<0)")
    if not kdj_states:
        kdj_states.append("中性区间")

    # ---------- RSI 状态与背离 ----------
    r6s = rsi(close, 6)
    r6 = float(r6s.iloc[-1])
    r12 = float(rsi(close, 12).iloc[-1])
    r24 = float(rsi(close, 24).iloc[-1])
    if r6 > 80:
        rsi_state = "极度超买(>80)"
    elif r6 > 70:
        rsi_state = "超买(>70)"
    elif r6 < 20:
        rsi_state = "极度超卖(<20)"
    elif r6 < 30:
        rsi_state = "超卖(<30)"
    else:
        rsi_state = "中性区间"

    div = None
    if bis:
        fin = [b for b in bis if not b.get("unfinished")]
        downs = [b for b in fin if b["dir"] == "down"]
        ups = [b for b in fin if b["dir"] == "up"]

        def rsi_at(idx):
            return float(r6s.iloc[idx]) if idx < len(r6s) else None

        cands = []
        if len(downs) >= 2:
            b1, b2 = downs[-2], downs[-1]
            v1, v2 = rsi_at(b1["end_idx"]), rsi_at(b2["end_idx"])
            if v1 is not None and v2 is not None:
                if b2["end_price"] < b1["end_price"] and v2 > v1:
                    cands.append((b2["end_idx"], {"type": "常规底背离", "side": "buy",
                                 "note": "价创新低而RSI抬高 → 反转信号,对应一类买点"}))
                elif b2["end_price"] > b1["end_price"] and v2 < v1:
                    cands.append((b2["end_idx"], {"type": "隐藏多头背离", "side": "buy",
                                 "note": "回调低点抬高而RSI更低 → 上涨中继,对应二/三类买点"}))
        if len(ups) >= 2:
            b1, b2 = ups[-2], ups[-1]
            v1, v2 = rsi_at(b1["end_idx"]), rsi_at(b2["end_idx"])
            if v1 is not None and v2 is not None:
                if b2["end_price"] > b1["end_price"] and v2 < v1:
                    cands.append((b2["end_idx"], {"type": "常规顶背离", "side": "sell",
                                 "note": "价创新高而RSI降低 → 反转信号,对应一类卖点"}))
                elif b2["end_price"] < b1["end_price"] and v2 > v1:
                    cands.append((b2["end_idx"], {"type": "隐藏空头背离", "side": "sell",
                                 "note": "反弹高点降低而RSI更高 → 下跌中继,对应二/三类卖点"}))
        if cands:
            div = max(cands, key=lambda x: x[0])[1]  # 取最近的信号

    rnd = lambda v: round(v, 2)
    return {
        "volume": {
            "ratio": rnd(ratio), "state": vol_state,
            "combo": combo, "combo_note": combo_note,
            "zone": zone, "pos": rnd(pos * 100),
            "zhuli": {"conclusion": concl, "evidence": evidence},
        },
        "kdj": {"k": rnd(kv), "d": rnd(dv), "j": rnd(jv), "states": kdj_states},
        "rsi": {"r6": rnd(r6), "r12": rnd(r12), "r24": rnd(r24),
                "state": rsi_state, "divergence": div},
    }


def macd_hist_list(candles: list[dict]) -> list[float]:
    """仅返回 MACD 柱序列(背驰面积计算用),NaN 置 0。"""
    close = pd.Series([c["c"] for c in candles], dtype=float)
    _, _, hist = macd(close)
    return [0.0 if np.isnan(v) else float(v) for v in hist.tolist()]
