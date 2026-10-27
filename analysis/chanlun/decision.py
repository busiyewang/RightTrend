"""综合交易决策:大级别定方向 + 本级别定买点的单级别决策。

思路(手册第 3、7-9 章):
- 大级别定方向:上一级别的结构状态(现价相对中枢位置 + 最新笔方向)给出方向与强度;
- 本级别定买点:最新锁定笔及其后的一二三类候选是否仍活跃;
- 指标确认:KDJ 金叉死叉、RSI 背离、量价组合作为确认票;
- 冲突规则:信号方向与大级别方向相反 → 禁止操作(观望);
- 失效规则:现价已越过信号点的结构止损位(做多跌破信号低点/做空突破信号高点,
  含 0.2% 缓冲)→ 信号作废,观望;
- 未锁定信号(端点仍可能随新K线重绘)质量分 ×0.7 降权,并附警告;
- 无结构目标 → 盈亏比无法核算,硬约束(≥1:2)无从满足 → 观望;
- 质量分 q = 方向强度×0.4 + 信号质量×0.4 + 确认度×0.2,映射仓位:
  q≥0.85→100%、≥0.7→70%、≥0.5→50%、≥0.35→30%、否则观望;
- 止损:结构止损(信号点下方 0.2%)与中枢止损(ZD 下方 0.2%)取更保守(离入场更近)者,
  另提示时间止损(12 根本级别K线未启动离场);
- 目标:第一目标=中枢 ZG/ZD,第二目标=近期高低点;盈亏比 <1:2 给出警告。
"""

# 方向级别映射:大级别定方向(参考手册级别链 1W→1D→4H→1H→15M→5M→1M)
LEVEL_UP = {"1m": "15m", "5m": "1H", "15m": "1H", "1H": "4H",
            "4H": "1D", "1D": "1W", "1W": "1W"}

STOP_BUFFER = 0.002  # 结构/中枢止损缓冲 0.2%


def active_signal_cutoff(finished):
    """保留刚锁定的端点，避免两笔确认滞后与旧的两笔活跃窗口互斥。

    更新的笔进入锁定前缀后，旧端点过期。没有锁定笔的早期窗口
    沿用最近两条成笔；方向、失效价位与评分等过滤仍由调用方检查。
    """
    if not finished:
        return float("inf")
    cutoff = finished[-2]["end_idx"] if len(finished) >= 2 else finished[-1]["start_idx"]
    locked = [b for b in finished if b.get("locked") is True]
    return min(cutoff, locked[-1]["end_idx"]) if locked else cutoff


def _direction(summary):
    """由上一级别结构给出方向与强度。"""
    bi = summary.get("last_bi")
    zs = summary.get("last_zs")
    pos = summary.get("price_pos")
    if not bi or not zs:
        return {"dir": "neutral", "strength": 0.3, "desc": "结构数据不足,视为震荡"}
    up = bi["dir"] == "up"
    if pos == "above_zg":
        if up:
            return {"dir": "long", "strength": 1.0, "desc": "现价在中枢上方且笔向上,上涨趋势"}
        return {"dir": "long", "strength": 0.6, "desc": "现价在中枢上方,笔回调中(多头趋势内回调)"}
    if pos == "below_zd":
        if not up:
            return {"dir": "short", "strength": 1.0, "desc": "现价在中枢下方且笔向下,下跌趋势"}
        return {"dir": "short", "strength": 0.6, "desc": "现价在中枢下方,笔反弹中(空头趋势内反弹)"}
    return {"dir": "neutral", "strength": 0.3, "desc": "现价在中枢内部,震荡"}


def _signal_quality(p, beichi):
    """买卖点信号质量 0-1:直接采用买卖点评分(0-100,越高越可靠)。"""
    if p.get("score") is not None:
        return round(min(p["score"], 100) / 100, 2)
    # 兼容无评分的旧数据:按手册可靠度 3类≈2类>1类
    t = p["type"][1]
    if t == "3":
        return 0.9
    if t == "2":
        return 0.8
    for bc in reversed(beichi):
        if bc["k_idx"] == p["k_idx"]:
            return 0.65 if bc["kind"] == "trend" else 0.5
    return 0.5


def decide(cur, hi, bar, hi_bar):
    ch = cur["chan"]
    sg = cur.get("signals") or {}
    candles = cur["candles"]
    live = candles[-1]["c"]
    fin = [b for b in ch["bi"] if not b.get("unfinished")]
    hd = _direction((hi["chan"] or {}).get("summary") or {})

    base = {
        "dir_level": hi_bar, "dir": hd["dir"], "dir_desc": hd["desc"],
        "rules": "手册硬约束:单笔风险0.5-1% · 杠杆3-10×逐仓 · 盈亏比≥1:2 · "
                 "同时持仓≤2 · 日亏3%/周亏6%停手",
    }

    # 兼容确认滞后：最新锁定端点及其后的候选才处于活跃窗口。
    active = None
    if ch["bsp"] and len(fin) >= 2:
        last = ch["bsp"][-1]
        if last["k_idx"] >= active_signal_cutoff(fin):
            active = last
    if not active:
        note = ""
        if ch["bsp"]:
            p = ch["bsp"][-1]
            note = f"最近信号 {p['type']}@{p['price']} 已不在活跃窗口(已有更近的结构端点)。"
        return {**base, "action": "wait", "q": 0,
                "reason": "本级别当前没有活跃的买卖点信号。" + note +
                          "等待新的背驰/买卖点出现再决策。"}

    side = "long" if active["type"][0] == "B" else "short"

    # 冲突:信号方向与大级别方向相反 → 手册规定禁止操作
    if (side == "long" and hd["dir"] == "short") or \
       (side == "short" and hd["dir"] == "long"):
        return {**base, "action": "wait", "q": 0, "signal": active,
                "reason": f"活跃信号 {active['type']} 为{'买' if side=='long' else '卖'}点,"
                          f"但{hi_bar}大级别方向相反({hd['desc']})。"
                          f"手册规定:信号与大级别冲突,禁止操作。"}

    # 价格失效:现价已越过信号点的结构止损位 → 买/卖点被击穿即作废
    #(做多:跌破信号低点;做空:突破信号高点。此时所有止损都在入场价的错误一侧)
    entry = live
    if side == "long" and entry <= active["price"] * (1 - STOP_BUFFER):
        return {**base, "action": "wait", "q": 0, "signal": active,
                "reason": f"信号 {active['type']}@{active['price']} 已失效:"
                          f"现价 {entry} 跌破信号低点(含0.2%缓冲)。"
                          f"买点被击穿即作废,等待新结构形成再决策。"}
    if side == "short" and entry >= active["price"] * (1 + STOP_BUFFER):
        return {**base, "action": "wait", "q": 0, "signal": active,
                "reason": f"信号 {active['type']}@{active['price']} 已失效:"
                          f"现价 {entry} 突破信号高点(含0.2%缓冲)。"
                          f"卖点被击穿即作废,等待新结构形成再决策。"}

    # 指标确认票(4 票制:当前 KDJ 叉 / RSI 背离 / 量价组合 / 信号点自身共振)
    confirms, hits = [], 0
    kdj_states = (sg.get("kdj") or {}).get("states") or []
    cross_ok = any(("金叉" if side == "long" else "死叉") in s for s in kdj_states)
    if cross_ok:
        confirms.append("KDJ " + ("金叉" if side == "long" else "死叉"))
        hits += 1
    div = (sg.get("rsi") or {}).get("divergence")
    if div and div.get("side") == ("buy" if side == "long" else "sell"):
        confirms.append(f"RSI {div['type']}")
        hits += 1
    combo = (sg.get("volume") or {}).get("combo") or ""
    good_combo = {"long": ("价跌量缩", "价涨量增"), "short": ("价涨量缩", "价跌量增")}[side]
    if combo in good_combo:
        confirms.append(f"量价配合({combo})")
        hits += 1
    # 信号点自身的指标共振(chan.find_bsp 计算:KDJ叉/钝化、RSI超卖超买、MACD柱拐头)
    sig_cf = active.get("confirms") or []
    if len(sig_cf) >= 2:
        confirms.append("信号点共振:" + "、".join(sig_cf))
        hits += 1
    if not confirms:
        confirms.append("暂无指标确认")

    sq = _signal_quality(active, ch["beichi"])
    # 未锁定信号(位于最新确认笔端点上)可能随更极端分型移动/消失 → 降权
    unlocked = not active.get("locked", True)
    if unlocked:
        sq = round(sq * 0.7, 2)
    q = round(hd["strength"] * 0.4 + sq * 0.4 + (hits / 4) * 0.2, 3)
    if q >= 0.85:
        pos_pct = 100
    elif q >= 0.7:
        pos_pct = 70
    elif q >= 0.5:
        pos_pct = 50
    elif q >= 0.35:
        pos_pct = 30
    else:
        return {**base, "action": "wait", "q": q, "signal": active,
                "reason": f"信号 {active['type']} 与大级别不冲突,但综合质量分 {q} 过低"
                          f"(方向强度 {hd['strength']}、信号质量 {sq}"
                          f"{',未锁定已×0.7降权' if unlocked else ''}、确认 {hits}/4),"
                          f"手册标准:q<0.35 观望。"}

    # 止损:结构止损 vs 中枢止损,取离入场更近(更保守)者
    #(前面已做价格失效检查,结构止损必在入场价的正确一侧)
    zs = (ch.get("summary") or {}).get("last_zs")
    stops = []
    if side == "long":
        stops.append(("结构止损(信号低点下0.2%)", active["price"] * (1 - STOP_BUFFER)))
        if zs and zs["zd"] * (1 - STOP_BUFFER) < entry:
            stops.append(("中枢止损(ZD下0.2%)", zs["zd"] * (1 - STOP_BUFFER)))
        stops = [s for s in stops if s[1] < entry]
        stop_name, stop = max(stops, key=lambda s: s[1])
        targets = []
        if zs and zs["zg"] > entry:
            targets.append(("中枢上沿 ZG", zs["zg"]))
        recent_hi = max(c["h"] for c in candles[-120:])
        if recent_hi > entry and (not targets or recent_hi > targets[0][1]):
            targets.append(("近期高点", recent_hi))
        risk = entry - stop
    else:
        stops.append(("结构止损(信号高点上0.2%)", active["price"] * (1 + STOP_BUFFER)))
        if zs and zs["zg"] * (1 + STOP_BUFFER) > entry:
            stops.append(("中枢止损(ZG上0.2%)", zs["zg"] * (1 + STOP_BUFFER)))
        stops = [s for s in stops if s[1] > entry]
        stop_name, stop = min(stops, key=lambda s: s[1])
        targets = []
        if zs and zs["zd"] < entry:
            targets.append(("中枢下沿 ZD", zs["zd"]))
        recent_lo = min(c["l"] for c in candles[-120:])
        if recent_lo < entry and (not targets or recent_lo < targets[0][1]):
            targets.append(("近期低点", recent_lo))
        risk = stop - entry

    rnd = lambda v: round(v, 6 if entry < 10 else 2)

    if risk <= 0:
        # 理论上被前面的失效检查拦截,防御性兜底
        return {**base, "action": "wait", "q": q, "signal": active,
                "stop": rnd(stop), "stop_name": stop_name,
                "reason": f"现价 {rnd(entry)} 已到达/越过止损位 {rnd(stop)},"
                          f"风险无法定义 → 观望。"}
    if not targets:
        # 无结构目标 → 盈亏比无法核算,硬约束(≥1:2)无从满足
        return {**base, "action": "wait", "q": q, "signal": active,
                "stop": rnd(stop), "stop_name": stop_name,
                "reason": f"信号 {active['type']} 有效(质量分 {q}),"
                          f"但{'上方' if side == 'long' else '下方'}无明确结构目标,"
                          f"盈亏比无法核算。手册硬约束盈亏比≥1:2 无从验证 → 观望,"
                          f"等中枢/结构目标形成后再评估。"}
    reward = abs(targets[0][1] - entry)
    rr = round(reward / risk, 2)
    if rr < 2:
        # 盈亏比≥1:2 是手册硬约束:不满足直接降级为观望,
        # 并给出满足 1:2 的入场价(entry* 满足 |目标-e|=2|e-止损| → e=(目标+2×止损)/3)
        better = (targets[0][1] + 2 * stop) / 3
        move = "回调" if side == "long" else "反弹"
        return {**base, "action": "wait", "q": q, "signal": active,
                "stop": rnd(stop), "stop_name": stop_name,
                "targets": [(n, rnd(v)) for n, v in targets], "rr": rr,
                "better_entry": rnd(better),
                "reason": f"信号 {active['type']}@{rnd(active['price'])} 有效(质量分 {q}),"
                          f"但现价 {rnd(entry)} 追单到第一目标的盈亏比仅 1:{rr},"
                          f"低于手册硬约束 1:2 → 观望,等{move}至 {rnd(better)} 附近再考虑"
                          f"(止损 {rnd(stop)},目标 {rnd(targets[0][1])})。"}

    warnings = []
    if unlocked:
        warnings.append("信号未锁定:端点可能随新K线移动或消失,评分已×0.7降权;"
                        "稳妥做法是等下一笔确认(信号锁定)后再入场。")
    return {
        **base,
        "action": side, "q": q, "position": pos_pct,
        "signal": active, "signal_quality": sq,
        "confirms": confirms, "confirm_hits": hits,
        "entry": rnd(entry), "stop": rnd(stop), "stop_name": stop_name,
        "time_stop": f"入场后 12 根 {bar} K线未按预期启动即离场(时间止损)",
        "targets": [(n, rnd(v)) for n, v in targets],
        "rr": rr, "warnings": warnings,
        "sizing": "仓位公式:R=账户权益×单笔风险%(0.5-1%),数量Q=R÷|入场-止损|,"
                  "保证金M=Q×入场价÷杠杆",
    }
