"""缠论结构算法:包含处理 → 分型 → 笔(新笔) → 中枢 → 背驰 → 一二三类买卖点。

规则依据《缠论MACD币圈实战手册.html》:
- 包含处理:向上取"高高",向下取"低低",方向由前一根未被包含 K 线决定。
- 顶分型 = 中间K高点最高且低点也最高(处理后K线);底分型反之。
- 新笔:两端点之间(含端点)≥5 根处理后K线;端点取分型中间K的极值;分型顶底交替,
  同类型取更极端者。
- 中枢:≥3 个连续段(相邻笔近似)重叠,ZG=min(各段高点),ZD=max(各段低点),ZG>ZD,可延伸。
- 背驰:趋势比较两个同向不重叠中枢的离开笔,盘整比较同一已形成中枢的同向波动;
  同向 MACD 柱面积后/前 < 0.7 且创新极值。无可验证中枢结构只记动能衰减。
- 买卖点:1买=趋势底背驰低点;2买=其后回调不破前低;3买=离开中枢回踩不跌回 ZG 之下。
"""

from .indicators import kdj, macd, macd_hist_list, rsi
import pandas as pd

# 背驰评分器级别权重(手册 LVL_W;15m 手册未给,取 0.8 插值)
LEVEL_WEIGHT = {"1m": 0.55, "5m": 0.7, "15m": 0.8, "1H": 0.9,
                "4H": 1.0, "1D": 1.0, "1W": 1.0}


# 结构模块独立于指标评分，保留这些导出供现有调用使用。
from .chan_strokes import merge_klines, find_fenxing, build_bi, _can_form_bi
from .chan_centers import build_zhongshu, logical_centers, third_point_candidates
from .chan_segments import build_segments


def _bi_high(b):
    return max(b["start_price"], b["end_price"])


def _bi_low(b):
    return min(b["start_price"], b["end_price"])


def _seg_area(hist, b, direction):
    """一笔区间内 MACD 同向柱面积:down 取负柱绝对值和,up 取正柱和。"""
    s = 0.0
    for v in hist[b["start_idx"]: b["end_idx"] + 1]:
        if direction == "down" and v < 0:
            s += -v
        elif direction == "up" and v > 0:
            s += v
    return s


def _touches_center(b, z):
    return _bi_high(b) >= z["zd"] and _bi_low(b) <= z["zg"]


def _leaves_center(b, z):
    """离开笔穿过中枢边界;它本身可能仍被中枢延伸纳入 bi_end。"""
    if b["dir"] == "down":
        return b["start_price"] >= z["zd"] and b["end_price"] < z["zd"]
    return b["start_price"] <= z["zg"] and b["end_price"] > z["zg"]


def _divergence_comparison(bis, i, centers):
    """先按结构选比较笔,再检验力度;结构不达标不能改比别的笔来生成一买。"""
    bout = bis[i]
    previous = i - 2
    active = centers[-1] if centers and centers[-1]["bi_end"] >= i - 2 else None
    if active and len(centers) >= 2 and _leaves_center(bout, active):
        first, second = centers[-2:]
        same_direction = (first["zd"] > second["zg"] if bout["dir"] == "down"
                          else first["zg"] < second["zd"])
        if same_direction:
            # 九笔封顶后才离开的笔也有效。它还可能成为第二中枢的首笔
            # (第一中枢的离开段 / 第二中枢的进入段),因此包含 second.bi_start。
            departures = [j for j in range(first["formed_bi"] + 1,
                                           second["bi_start"] + 1)
                          if not bis[j]["unfinished"] and bis[j]["dir"] == bout["dir"]
                          and _leaves_center(bis[j], first)]
            if departures:
                return departures[-1], "trend", [first, second], (
                    "比较两个同向不重叠中枢各自的离开笔;中枢仅取后笔开始前已形成的结构")

    if active and active["formed_bi"] < previous and \
            previous >= active["bi_start"] and \
            _touches_center(bis[previous], active) and _touches_center(bout, active):
        return previous, "panzheng", [active], (
            "比较同一已形成中枢关联的两次同向波动;只提示局部力度衰减")

    return previous, "momentum", [], (
        "相邻同向笔力度衰减,但无法验证两个中枢离开段或同一已形成中枢的比较结构")


def _comparison_leg(b, bi_idx):
    return dict({key: b[key] for key in ("start_ts", "end_ts", "start_price", "end_price",
                                        "start_idx", "end_idx")}, bi_idx=bi_idx)


def detect_beichi(bis, zss, candles, bar):
    """按候选当时的笔前缀识别 trend / panzheng / momentum。

    保留 zss 参数兼容现有调用,但最终中枢会包含之后的延伸,不能用于历史分类。
    每笔开始前重建已形成中枢,先据结构选择比较对象,然后检验创新极值和面积比。
    仍以笔近似次级别走势,不是完整线段级别的递归缠论。
    comparison 保存实际比较笔、中枢快照和判断时点,供图表核对。
    """
    close = pd.Series([c["c"] for c in candles], dtype=float)
    dif_s, _, _ = macd(close)
    dif = dif_s.tolist()
    hist = macd_hist_list(candles)
    lvl_w = LEVEL_WEIGHT.get(bar, 1.0)

    out = []
    for i in range(2, len(bis)):
        bout = bis[i]
        if bout["unfinished"] or bis[i - 2]["unfinished"]:
            continue
        centers = logical_centers(build_zhongshu(bis[:i], candles))
        before_idx, kind, related, reason = _divergence_comparison(bis, i, centers)
        bin_ = bis[before_idx]
        if bin_["dir"] != bout["dir"] or bin_["unfinished"]:
            continue
        d = bout["dir"]
        # 门槛:后段须创新低/新高
        if d == "down" and not _bi_low(bout) < _bi_low(bin_):
            continue
        if d == "up" and not _bi_high(bout) > _bi_high(bin_):
            continue
        if kind == "trend":
            # 必须是这一轮趋势的新极值;不能在更低低点之后的反弹回踩上
            # 继续对旧中枢离开段比力度,将更高低点误标为又一个一买。
            preceding = bis[before_idx:i]
            if d == "down" and bout["end_price"] >= min(_bi_low(b) for b in preceding):
                continue
            if d == "up" and bout["end_price"] <= max(_bi_high(b) for b in preceding):
                continue
        a_in = _seg_area(hist, bin_, d)
        a_out = _seg_area(hist, bout, d)
        if a_in <= 0:
            continue
        ratio = a_out / a_in
        if ratio >= 0.7:
            continue  # 手册:<0.7 才是有效背驰

        struct_ok = kind != "momentum"
        score, detail = _score_beichi(ratio, kind, bin_, bout, dif, candles, lvl_w)
        if not struct_ok:
            score = round(score * 0.85, 1)
        out.append({
            "kind": kind, "dir": d,
            "k_idx": bout["end_idx"], "price": bout["end_price"],
            "ts": bout["end_ts"], "area_ratio": round(ratio, 4),
            "score": score, "score_detail": detail,
            "bi_idx": i, "struct_ok": struct_ok,
            "area_in": round(a_in, 8), "area_out": round(a_out, 8), "reason": reason,
            "comparison": {"before": _comparison_leg(bin_, before_idx),
                           "after": _comparison_leg(bout, i),
                           "centers": [dict(z) for z in related], "as_of_bi": i},
        })
    return out


def _score_beichi(ratio, kind, bin_, bout, dif, candles, lvl_w):
    """手册背驰评分器:面积比分段(≤0.4→30/≤0.6→26/≤0.7→20/≤0.85→11/<1→4/≥1→0)
    + DIF极值20 + 区间套20(单周期无法验证,恒0) + 趋势15 + 结构12 + 斜率8 + 量能7
    + 反向分型8,总权重120 归一到 100,乘级别权重。"""
    if ratio <= 0.4:
        s_area = 30
    elif ratio <= 0.6:
        s_area = 26
    elif ratio <= 0.7:
        s_area = 20
    elif ratio <= 0.85:
        s_area = 11
    elif ratio < 1.0:
        s_area = 4
    else:
        s_area = 0

    d = bout["dir"]
    din = dif[bin_["start_idx"]: bin_["end_idx"] + 1]
    dout = dif[bout["start_idx"]: bout["end_idx"] + 1]
    if d == "down":
        s_dif = 20 if min(dout) > min(din) else 0   # DIF 低点抬高
    else:
        s_dif = 20 if max(dout) < max(din) else 0   # DIF 高点降低

    s_sub = 0  # 区间套:单周期无法自动验证,保守记 0
    s_trend = 15 if kind == "trend" else 0
    s_struct = 12 if bout["mk_count"] >= 9 else 0  # 后段结构完整(处理后K≥9近似)

    bars_in = bin_["end_idx"] - bin_["start_idx"] + 1
    bars_out = bout["end_idx"] - bout["start_idx"] + 1
    slope_in = abs(bin_["end_price"] - bin_["start_price"]) / max(bars_in, 1)
    slope_out = abs(bout["end_price"] - bout["start_price"]) / max(bars_out, 1)
    s_slope = 8 if (bars_out >= bars_in and slope_out < slope_in) else 0

    vol_in = sum(c["vol"] for c in candles[bin_["start_idx"]: bin_["end_idx"] + 1])
    vol_out = sum(c["vol"] for c in candles[bout["start_idx"]: bout["end_idx"] + 1])
    s_vol = 7 if vol_out < vol_in else 0

    s_fx = 8 if not bout["unfinished"] else 0  # 笔已被反向分型确认

    raw = s_area + s_dif + s_sub + s_trend + s_struct + s_slope + s_vol + s_fx
    score = round(raw / 120 * 100 * lvl_w, 1)
    detail = {"area": s_area, "dif": s_dif, "sub": "未验证", "trend": s_trend,
              "struct": s_struct, "slope": s_slope, "vol": s_vol, "fx": s_fx}
    return score, detail


# ---------------------------------------------------------------- 指标背离

# 五票权重:MACD 柱最可靠,量能/KDJ 辅助(合计 78,再加超卖超买区 10、
# 创新极值确认 12,满分 100)
_DL_VOTE_W = {"MACD柱": 22, "DIF": 16, "RSI": 16, "KDJ": 12, "缩量": 12}


def detect_beili(bis, bcs, candles, bar):
    """指标背离信号(补充缠论背驰,门槛更宽、数量更多):
    比较相邻两个同向确认笔的端点。
    - 常规背离(反转):价创新低/新高,而指标拒绝新极值 → 类一类买卖点,≥2 票成立;
    - 隐藏背离(中继):价未创新极值(低点抬高/高点降低)而指标创出新极值
      → 顺原方向的回调结束信号,类二/三类买卖点,≥3 票成立(更严防噪音)。
    五票 = MACD柱 / DIF / RSI6 / KDJ-J / 量能,票越多越可靠。
    与已有结构背驰/动能衰减提示同点的常规背离不再重复输出。
    返回 [{type: DB|DS, subtype: regular|hidden, k_idx, price, ts,
           votes, score, grade, note}],评分 0-100 越高越好(乘级别权重)。"""
    if len(candles) < 30:
        return []
    df_c = pd.Series([c["c"] for c in candles], dtype=float)
    df_h = pd.Series([c["h"] for c in candles], dtype=float)
    df_l = pd.Series([c["l"] for c in candles], dtype=float)
    dif = macd(df_c)[0].tolist()
    hist = macd_hist_list(candles)
    r6 = rsi(df_c, 6).tolist()
    jv = kdj(df_h, df_l, df_c)[2].tolist()
    lvl_w = LEVEL_WEIGHT.get(bar, 1.0)
    bc_at = {b["k_idx"] for b in bcs}

    def seg_ext(arr, b, fn):
        return fn(arr[b["start_idx"]: b["end_idx"] + 1])

    def unit_vol(b):
        seg = candles[b["start_idx"]: b["end_idx"] + 1]
        return sum(c["vol"] for c in seg) / max(len(seg), 1)

    out = []
    for i in range(2, len(bis)):
        b1, b2 = bis[i - 2], bis[i]
        if b1["dir"] != b2["dir"] or b1["unfinished"] or b2["unfinished"]:
            continue
        d = b2["dir"]
        e1, e2 = b1["end_idx"], b2["end_idx"]
        buy = d == "down"
        new_ext = b2["end_price"] < b1["end_price"] if buy \
            else b2["end_price"] > b1["end_price"]
        # 指标在两端点/两段内的取值(段内取极值,端点取当根)
        if buy:
            h1, h2 = seg_ext(hist, b1, min), seg_ext(hist, b2, min)
            f1, f2 = seg_ext(dif, b1, min), seg_ext(dif, b2, min)
        else:
            h1, h2 = seg_ext(hist, b1, max), seg_ext(hist, b2, max)
            f1, f2 = seg_ext(dif, b1, max), seg_ext(dif, b2, max)
        rv1, rv2 = r6[e1], r6[e2]
        j1, j2 = jv[e1], jv[e2]
        v1, v2 = unit_vol(b1), unit_vol(b2)

        votes = []
        if new_ext:
            if e2 in bc_at:
                continue  # 与缠论背驰同点,不重复
            subtype = "regular"
            # 价新极值,指标拒绝确认(反向收敛)
            if (h2 > h1 and h1 < 0) if buy else (h2 < h1 and h1 > 0):
                votes.append("MACD柱")
            if (f2 > f1) if buy else (f2 < f1):
                votes.append("DIF")
            if (rv2 > rv1) if buy else (rv2 < rv1):
                votes.append("RSI")
            if (j2 > j1) if buy else (j2 < j1):
                votes.append("KDJ")
            if v2 < v1:
                votes.append("缩量")
            if len(votes) < 2:
                continue
            word = "低" if buy else "高"
            note = (f"价创新{word}而 {'/'.join(votes)} 拒绝新{word}"
                    f"(常规{'底' if buy else '顶'}背离,反转信号)")
        else:
            subtype = "hidden"
            # 价未新极值(回调),指标却更极端 → 动能被回调消化,原方向未完
            if (h2 < h1) if buy else (h2 > h1):
                votes.append("MACD柱")
            if (f2 < f1) if buy else (f2 > f1):
                votes.append("DIF")
            if (rv2 < rv1) if buy else (rv2 > rv1):
                votes.append("RSI")
            if (j2 < j1) if buy else (j2 > j1):
                votes.append("KDJ")
            if v2 < v1:
                votes.append("缩量")
            if len(votes) < 3:
                continue
            note = (f"{'回调低点抬高' if buy else '反弹高点降低'}而指标更弱"
                    f"({'/'.join(votes)}),隐藏{'多头' if buy else '空头'}背离,"
                    f"{'上涨' if buy else '下跌'}中继——须与大级别方向同向才可用")

        raw = sum(_DL_VOTE_W[v] for v in votes)
        if (rv2 < 30) if buy else (rv2 > 70):
            raw += 10  # 超卖/超买区共振
        if subtype == "regular":
            raw += 12  # 创出新极值后被拒绝,反转意义更明确
        score = round(min(100.0, raw) * lvl_w, 1)
        out.append({
            "type": "DB" if buy else "DS", "subtype": subtype,
            "k_idx": e2, "price": b2["end_price"], "ts": b2["end_ts"],
            "votes": votes, "score": score, "grade": _grade(score),
            "note": note, "source_bi_idx": i,
        })
    out.sort(key=lambda p: p["k_idx"])
    return out


# ---------------------------------------------------------------- 买卖点

def _grade(score):
    return "高" if score >= 75 else ("中" if score >= 60 else
                                     ("一般" if score >= 45 else "弱"))


def find_bsp(bis, zss, bcs, candles, bar):
    """一二三类买卖点(带评分,0-100,越高越可靠)。
    评分 = 类型基础分(手册可靠度 3类≈2类>1类)+ 结构质量 + 量能配合,
    最后乘级别权重(级别越大越可靠)。
    返回 [{type, k_idx, price, ts, note, score, grade}]。"""
    out = []
    fin = bis
    lvl_w = LEVEL_WEIGHT.get(bar, 1.0)

    # 共振确认用指标序列(信号点处的独立佐证,不改变手册评分口径)
    close_s = pd.Series([c["c"] for c in candles], dtype=float)
    high_s = pd.Series([c["h"] for c in candles], dtype=float)
    low_s = pd.Series([c["l"] for c in candles], dtype=float)
    k_s, d_s, j_s = kdj(high_s, low_s, close_s)
    kv, dv, jv = k_s.tolist(), d_s.tolist(), j_s.tolist()
    r6 = rsi(close_s, 6).tolist()
    hist = macd_hist_list(candles)

    def confirms_at(type_, k_idx):
        """信号点±2根内的指标共振票:KDJ叉 / KDJ钝化 / RSI超卖超买 / MACD柱拐头。
        未锁定的最新信号右侧K线还没走出来,票数会偏少,属正常。"""
        buy = type_[0] == "B"
        cf = []
        lo, hi = max(1, k_idx - 2), min(len(candles) - 1, k_idx + 2)
        for t in range(lo, hi + 1):
            if buy and kv[t - 1] <= dv[t - 1] and kv[t] > dv[t]:
                cf.append("KDJ金叉")
                break
            if not buy and kv[t - 1] >= dv[t - 1] and kv[t] < dv[t]:
                cf.append("KDJ死叉")
                break
        if buy and jv[k_idx] < 0:
            cf.append("J值超卖钝化")
        elif not buy and jv[k_idx] > 100:
            cf.append("J值超买钝化")
        if buy and r6[k_idx] < 30:
            cf.append("RSI超卖")
        elif not buy and r6[k_idx] > 70:
            cf.append("RSI超买")
        nxt = min(k_idx + 2, len(candles) - 1)
        if nxt > k_idx:
            if buy and hist[nxt] > hist[k_idx]:
                cf.append("MACD柱拐头向上")
            elif not buy and hist[nxt] < hist[k_idx]:
                cf.append("MACD柱拐头向下")
        return cf

    def vol_rate(b):
        """笔区间的单位K线均量(归一化,便于不同长度笔比较)。"""
        seg = candles[b["start_idx"]: b["end_idx"] + 1]
        return sum(c["vol"] for c in seg) / max(len(seg), 1)

    def emit(type_, k_idx, price, ts, note, raw, **metadata):
        score = min(100.0, round(raw * lvl_w, 1))
        cf = confirms_at(type_, k_idx)
        out.append({"type": type_, "k_idx": k_idx, "price": price, "ts": ts,
                    "note": note, "score": score, "grade": _grade(score),
                    "confirms": cf, "confirm_n": len(cf), **metadata})

    # 1/2类仅由结构已验证的趋势背驰产生;盘整和无结构动能提示不得冒充一买。
    for bc in bcs:
        if bc.get("kind") != "trend" or bc.get("struct_ok") is not True:
            continue
        bc_raw = bc["score"] / lvl_w if lvl_w > 0 else bc["score"]  # 还原未加权分
        raw1 = 40 + bc_raw * 0.4
        if bc["dir"] == "down":
            emit("B1", bc["k_idx"], bc["price"], bc["ts"],
                 f"趋势底背驰,面积比{bc['area_ratio']},背驰评分{bc['score']}", raw1, source_bi_idx=bc["bi_idx"])
        else:
            emit("S1", bc["k_idx"], bc["price"], bc["ts"],
                 f"趋势顶背驰,面积比{bc['area_ratio']},背驰评分{bc['score']}", raw1, source_bi_idx=bc["bi_idx"])

        # 2类:1类点之后,次级别回调/反弹不破前极值
        i = bc["bi_idx"]
        if i + 2 < len(fin) and not fin[i + 2]["unfinished"]:
            b_imp, b_back = fin[i + 1], fin[i + 2]
            imp_rng = abs(b_imp["end_price"] - b_imp["start_price"])
            back_rng = abs(b_back["end_price"] - b_back["start_price"])
            retr = back_rng / imp_rng if imp_rng > 0 else 1.0
            raw2 = 50 + (18 if retr <= 0.382 else 10 if retr <= 0.618 else 4)
            raw2 += bc_raw * 0.15                       # 联动前面1类点的背驰强度
            if vol_rate(b_back) < vol_rate(b_imp):
                raw2 += 8                               # 回调/反弹缩量
            extra = f"回撤{retr:.0%}" + (",缩量" if vol_rate(b_back) < vol_rate(b_imp) else "")
            if bc["dir"] == "down" and b_back["dir"] == "down" \
                    and b_back["end_price"] > bc["price"]:
                emit("B2", b_back["end_idx"], b_back["end_price"], b_back["end_ts"],
                     f"1买后回调不破前低 {bc['price']}({extra})", raw2, source_bi_idx=i + 2)
            if bc["dir"] == "up" and b_back["dir"] == "up" \
                    and b_back["end_price"] < bc["price"]:
                emit("S2", b_back["end_idx"], b_back["end_price"], b_back["end_ts"],
                     f"1卖后反弹不过前高 {bc['price']}({extra})", raw2, source_bi_idx=i + 2)

    # 离开笔可与蓝框相交；来源中枢必须在它之前已经成立。
    for candidate in third_point_candidates(bis, candles):
        c = dict(candidate)
        type_ = c.pop("type")
        emit(type_, c.pop("k_idx"), c.pop("price"), c.pop("ts"),
             c.pop("note"), c.pop("raw"), **c)

    out.sort(key=lambda p: p["k_idx"])
    return out


# ---------------------------------------------------------------- 汇总

def summarize(bis, zss, bcs, bsp, candles):
    last_price = candles[-1]["c"] if candles else None
    s = {"last_price": last_price, "last_bi": None, "last_zs": None,
         "price_pos": None, "last_beichi": bcs[-1] if bcs else None,
         "last_bsp": bsp[-1] if bsp else None}
    fin = [b for b in bis if not b["unfinished"]]
    if fin:
        b = fin[-1]
        s["last_bi"] = {"dir": b["dir"], "start_price": b["start_price"],
                        "end_price": b["end_price"], "end_ts": b["end_ts"],
                        "locked": b["locked"], "state": b["state"]}
    if zss:
        z = zss[-1]
        extending = z.get("extending", z["bi_end"] >= len(fin) - 1 if fin else False)
        s["last_zs"] = {"zg": z["zg"], "zd": z["zd"], "gg": z["gg"], "dd": z["dd"],
                        "extending": extending, **{key: z.get(key) for key in
                        ("locked", "state", "bi_count", "extension_pending", "known_idx", "known_at",
                         "span_known_idx", "span_known_at", "locked_idx", "locked_at")}}
        if last_price is not None:
            s["price_pos"] = "above_zg" if last_price > z["zg"] else \
                             ("below_zd" if last_price < z["zd"] else "inside")
    return s


BAR_MS = {"1m": 60000, "5m": 300000, "15m": 900000, "1H": 3600000,
          "4H": 14400000, "1D": 86400000, "1W": 604800000}


def _signal_availability(signals, bis, candles, with_confirms=False):
    """继承来源笔的真实锁定事件；端点早于最新笔不等于已经锁定。"""
    by_end = {b["end_idx"]: i for i, b in enumerate(bis) if not b["unfinished"]}
    for signal in signals:
        index = signal.get("source_bi_idx", signal.get("bi_idx", by_end.get(signal["k_idx"])))
        if index is None:
            signal.update(locked=False, state="provisional", locked_idx=None, locked_at=None)
            continue
        source = bis[index]
        signal["source_bi_idx"] = index
        # 三类点自身的中枢依赖也必须已锁定。
        locked = source.get("locked", False) and signal.get("locked", True)
        known = max(source["known_idx"], signal.get("known_idx", 0))
        if with_confirms:
            known = max(known, min(signal["k_idx"] + 2, len(candles) - 1))
        signal.update(known_idx=known, locked=locked,
                      state="locked" if locked else "provisional")
        if locked:
            signal["locked_idx"] = max(known, source["locked_idx"], signal.get("locked_idx") or 0)
        else:
            signal["locked_idx"] = None
            signal["locked_at"] = None


def _normalize_event_times(value, candles, duration):
    """K线 ts 是开盘时间，事件在对应K线收盘才可知；保留极值锚点时间。"""
    if isinstance(value, dict):
        for key, idx in list(value.items()):
            if key.endswith(("known_idx", "locked_idx")):
                value[key[:-4] + "_at"] = (candles[idx].get("close_ts", candles[idx]["ts"] + duration)
                                           if isinstance(idx, int) and 0 <= idx < len(candles) else None)
        for child in value.values():
            _normalize_event_times(child, candles, duration)
    elif isinstance(value, list):
        for child in value:
            _normalize_event_times(child, candles, duration)


def analyze(candles, bar):
    """主入口。candles 为升序、仅含已完结K线。"""
    if len(candles) < 10:
        return {"fenxing": [], "bi": [], "xianduan": [], "zhongshu": [],
                "zhongshu_display": [], "beichi": [], "beili": [], "bsp": [], "summary": {}}
    merged = merge_klines(candles)
    fenxing = find_fenxing(merged, candles)
    bis, _ = build_bi(fenxing, candles, merged)
    zss = build_zhongshu(bis, candles)
    display_zss = logical_centers(zss)
    segments = build_segments(bis, candles)
    bcs = detect_beichi(bis, display_zss, candles, bar)
    bli = detect_beili(bis, bcs, candles, bar)
    bsp = find_bsp(bis, display_zss, bcs, candles, bar)
    _signal_availability(bcs, bis, candles)
    _signal_availability(bli, bis, candles)
    _signal_availability(bsp, bis, candles, with_confirms=True)
    summary = summarize(bis, display_zss, bcs, bsp, candles)
    result = {"fenxing": fenxing, "bi": bis, "xianduan": segments,
              "zhongshu": zss, "zhongshu_display": display_zss,
              "beichi": bcs, "beili": bli, "bsp": bsp, "summary": summary}
    _normalize_event_times(result, candles, BAR_MS[bar])
    return result
