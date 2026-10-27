"""笔级近似中枢及三类点：原子中枢最多九笔，连续震荡单独分组。

这里的连续组不是严格缠论的高级别中枢。它只保留首个三笔区间，
避免九笔计数上限把尚未离开的同一震荡过程误当作独立趋势中枢。
known_at 使用 known_idx 对应 K 线的原始时间；API 层负责转换收盘时刻。
"""


MAX_ATOMIC_STROKES = 9


def _high(b):
    return max(b["start_price"], b["end_price"])


def _low(b):
    return min(b["start_price"], b["end_price"])


def _touches(b, zd, zg):
    return _high(b) >= zd and _low(b) <= zg


def _locked(b):
    # 旧的手工笔 fixture 没有锁定字段，沿用其明确的完整笔约定。
    return bool(b.get("locked", not b.get("unfinished", False)))


def _known(b):
    value = b.get("known_idx")
    return b["end_idx"] if value is None else value


def _known_fields(strokes, candles):
    b = max(strokes, key=_known)
    idx = _known(b)
    stamp = candles[idx]["ts"] if 0 <= idx < len(candles) else b.get("known_at", b["end_ts"])
    return {"known_idx": idx, "known_at": stamp}


def _locked_fields(strokes, candles):
    if not all(_locked(b) for b in strokes):
        return {"locked_idx": None, "locked_at": None}

    def event_idx(b):
        return b.get("locked_idx") if b.get("locked_idx") is not None else _known(b)

    b = max(strokes, key=event_idx)
    idx = event_idx(b)
    stamp = candles[idx]["ts"] if 0 <= idx < len(candles) else b.get("locked_at", b["end_ts"])
    return {"locked_idx": idx, "locked_at": stamp}


def build_zhongshu(bis, candles):
    """返回原子中枢；逻辑区间请使用 logical_centers 的去重视图。

    每个原子由前三笔确定 ZG/ZD，最多九笔；后续仍与首区间重叠的笔
    共享 group_id。九笔后尚不足三笔时不虚构新原子，但更新组的右边界
    并置 extension_pending。原子区间/笔数和连续组区间/笔数明确分开。
    """
    fin = [(idx, b) for idx, b in enumerate(bis) if not b.get("unfinished", False)]
    out = []
    i = 0
    while i + 2 < len(fin):
        first_three = [b for _, b in fin[i:i + 3]]
        zg = min(_high(b) for b in first_three)
        zd = max(_low(b) for b in first_three)
        if zg <= zd:
            i += 1
            continue

        # 先确定同一区间的连续过程，再切九笔原子。封顶不代表离开。
        end = i + 3
        while end < len(fin) and _touches(fin[end][1], zd, zg):
            end += 1
        group = fin[i:end]
        group_strokes = [b for _, b in group]
        first, last = group_strokes[0], group_strokes[-1]
        group_id = group[0][0]
        group_known = _known_fields(first_three, candles)
        group_span_known = _known_fields(group_strokes, candles)
        group_lock_event = _locked_fields(group_strokes, candles)
        group_locked = all(_locked(b) for b in group_strokes)
        extending = end == len(fin)
        pending = len(group) > MAX_ATOMIC_STROKES and len(group) % MAX_ATOMIC_STROKES in (1, 2)
        group_fields = {
            "group_id": group_id, "group_zg": zg, "group_zd": zd,
            "group_gg": max(_high(b) for b in group_strokes),
            "group_dd": min(_low(b) for b in group_strokes),
            "group_bi_start": group_id, "group_bi_end": group[-1][0],
            "group_bi_count": len(group),
            "group_start_idx": first["start_idx"], "group_end_idx": last["end_idx"],
            "group_start_ts": first["start_ts"], "group_end_ts": last["end_ts"],
            "group_formed_bi": group[2][0],
            "group_formed_extreme_ts": first_three[-1]["end_ts"],
            "group_known_idx": group_known["known_idx"],
            "group_known_at": group_known["known_at"],
            "group_span_known_idx": group_span_known["known_idx"],
            "group_span_known_at": group_span_known["known_at"],
            "group_locked": group_locked,
            "group_locked_idx": group_lock_event["locked_idx"],
            "group_locked_at": group_lock_event["locked_at"],
            "extension_pending": pending, "extending": extending,
        }
        for offset in range(0, len(group), MAX_ATOMIC_STROKES):
            atom = group[offset:offset + MAX_ATOMIC_STROKES]
            if len(atom) < 3:
                break
            strokes = [b for _, b in atom]
            forming = strokes[:3]
            atom_zg = min(_high(b) for b in forming)
            atom_zd = max(_low(b) for b in forming)
            if atom_zg <= atom_zd:
                # 退化接触不能生成零高度原子；组本身仍保留这段延伸。
                continue
            locked = all(_locked(b) for b in strokes)
            span_known = _known_fields(strokes, candles)
            out.append(dict(group_fields, **{
                "zg": atom_zg, "zd": atom_zd,
                "gg": max(_high(b) for b in strokes),
                "dd": min(_low(b) for b in strokes),
                "bi_start": atom[0][0], "bi_end": atom[-1][0],
                "formed_bi": atom[2][0], "formed_extreme_ts": forming[-1]["end_ts"],
                "bi_count": len(atom),
                "start_idx": strokes[0]["start_idx"], "end_idx": strokes[-1]["end_idx"],
                "start_ts": strokes[0]["start_ts"], "end_ts": strokes[-1]["end_ts"],
                "continuation_of": group_id if offset else None,
                "locked": locked, "state": "locked" if locked else "provisional",
                "span_known_idx": span_known["known_idx"],
                "span_known_at": span_known["known_at"],
                **_known_fields(forming, candles),
                **_locked_fields(strokes, candles),
            }))
        i = end
    return out


def logical_centers(zss):
    """每个连续组一条，固定首区间，扩展右边界；兼容旧式中枢字典。"""
    out = []
    seen = set()
    for z in zss:
        key = z.get("group_id", z["bi_start"])
        if key in seen:
            continue
        seen.add(key)
        logical = dict(z)
        logical["atomic_bi_count"] = z["bi_count"]
        logical["atomic_count"] = sum(1 for other in zss
                                      if other.get("group_id", other["bi_start"]) == key)
        for field in ("zg", "zd", "gg", "dd", "bi_start", "bi_end", "bi_count",
                      "start_idx", "end_idx", "start_ts", "end_ts", "formed_bi",
                      "formed_extreme_ts", "known_idx", "known_at", "locked",
                      "span_known_idx", "span_known_at", "locked_idx", "locked_at"):
            group_field = "group_" + field
            if group_field in z:
                logical[field] = z[group_field]
        logical["state"] = "locked" if logical.get("locked", True) else "provisional"
        logical["is_continuation_group"] = logical["bi_count"] > MAX_ATOMIC_STROKES
        out.append(logical)
    return out


def third_point_candidates(bis, candles):
    """按真实离开笔和紧接回踩笔返回 B3/S3 原始分，供主评分器 emit。

    每个候选只读取离开笔之前的笔前缀，避免最终中枢吞掉离开笔造成
    索引错位，也不允许离开/回踩笔自己组成的中枢为自身提供结构依据。
    """
    out = []
    seen = set()

    def volume_rate(b):
        part = candles[b["start_idx"]:b["end_idx"] + 1]
        return sum(c.get("vol", 0) for c in part) / max(len(part), 1)

    for back_idx in range(4, len(bis)):
        leave_idx = back_idx - 1
        leave, back = bis[leave_idx], bis[back_idx]
        if leave.get("unfinished", False) or back.get("unfinished", False):
            continue
        if back["dir"] == leave["dir"] or leave["end_idx"] != back["start_idx"]:
            continue
        prior = logical_centers(build_zhongshu(bis[:leave_idx], candles))
        if not prior:
            continue
        z = prior[-1]
        # 只评价仍与当前离开动作衔接的中枢，不能反复复用远处旧区间。
        if z["bi_end"] != leave_idx - 1 or z["formed_bi"] >= leave_idx:
            continue
        known = _known_fields([leave, back], candles)
        if max(z.get("known_idx", -1), z.get("span_known_idx", -1)) > known["known_idx"]:
            continue
        type_ = None
        if (leave["dir"] == "up" and back["dir"] == "down"
                and leave["start_price"] <= z["zg"] < leave["end_price"]
                and back["end_price"] > z["zg"]):
            type_, boundary = "B3", z["zg"]
        elif (leave["dir"] == "down" and back["dir"] == "up"
                and leave["start_price"] >= z["zd"] > leave["end_price"]
                and back["end_price"] < z["zd"]):
            type_, boundary = "S3", z["zd"]
        if type_ is None:
            continue
        key = (type_, back["end_ts"])
        if key in seen:
            continue
        seen.add(key)
        depth = abs(back["end_price"] - boundary) / abs(boundary) if boundary else float("inf")
        power = abs(leave["end_price"] - leave["start_price"]) / (z["zg"] - z["zd"])
        shrink = volume_rate(back) < volume_rate(leave)
        raw = 55 + (15 if depth <= 0.01 else 8 if depth <= 0.03 else 3)
        raw += 15 if power >= 1.5 else 8 if power >= 0.8 else 3
        raw += 8 if shrink else 0
        note = (f"回踩不破中枢上沿 ZG={boundary}" if type_ == "B3"
                else f"回抽不上中枢下沿 ZD={boundary}")
        locked = _locked(leave) and _locked(back) and z.get("locked", True)
        lock_event = {"locked_idx": None, "locked_at": None}
        if locked:
            lock_event = _locked_fields([leave, back], candles)
            if z.get("locked_idx") is not None and z["locked_idx"] > lock_event["locked_idx"]:
                lock_event = {"locked_idx": z["locked_idx"], "locked_at": z["locked_at"]}
        out.append({
            "type": type_, "k_idx": back["end_idx"], "price": back["end_price"],
            "ts": back["end_ts"], "note": note + (",缩量" if shrink else ""),
            "raw": raw, "source_zs": z, "source_bi_idx": back_idx,
            "leave_bi_idx": leave_idx, "back_bi_idx": back_idx,
            "locked": locked, "state": "locked" if locked else "provisional",
            **known,
            **lock_event,
        })
    return out
