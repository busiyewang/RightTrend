"""Closed-candle inclusion, fractals and conservative five-bar strokes.

An endpoint may lock after two later confirmed strokes if it is still the
same-type extreme of the adjustable suffix. Locked endpoints are an immutable
prefix; an incompatible tail remains explicitly blocked instead of forcing an
invalid stroke. ``known_idx`` and ``locked_idx`` identify closed-candle events,
not the earlier candle on which an extreme occurred. Their ``*_at`` values use
that candle's timestamp; callers add the bar duration when timestamps are opens.
"""


def _event_at(candles, idx):
    return candles[idx].get("close_ts", candles[idx]["ts"])


def merge_klines(candles):
    """Merge inclusions, retaining original extreme and source-span indices."""
    merged = []
    direction = 1  # The left edge has no preceding trend; retain project policy.
    for i, k in enumerate(candles):
        if not k.get("confirm", 1):
            continue
        h, l = k["h"], k["l"]
        if not merged:
            merged.append(dict(h=h, l=l, hi_idx=i, lo_idx=i,
                               idx_start=i, idx_end=i))
            continue
        last = merged[-1]
        contains = ((last["h"] >= h and last["l"] <= l) or
                    (h >= last["h"] and l <= last["l"]))
        if contains:
            if direction > 0:
                if h > last["h"]:
                    last["h"], last["hi_idx"] = h, i
                if l > last["l"]:
                    last["l"], last["lo_idx"] = l, i
            else:
                if h < last["h"]:
                    last["h"], last["hi_idx"] = h, i
                if l < last["l"]:
                    last["l"], last["lo_idx"] = l, i
            last["idx_end"] = i
        else:
            direction = 1 if h > last["h"] else -1
            merged.append(dict(h=h, l=l, hi_idx=i, lo_idx=i,
                               idx_start=i, idx_end=i))
    return merged


def find_fenxing(merged, candles):
    """The first candle of the right processed bar makes a fractal knowable."""
    out = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1:i + 2]
        if (b["h"] > a["h"] and b["h"] > c["h"] and
                b["l"] > a["l"] and b["l"] > c["l"]):
            type_, k_idx, price = "top", b["hi_idx"], b["h"]
        elif (b["l"] < a["l"] and b["l"] < c["l"] and
              b["h"] < a["h"] and b["h"] < c["h"]):
            type_, k_idx, price = "bottom", b["lo_idx"], b["l"]
        else:
            continue
        known_idx = c["idx_start"]
        out.append(dict(type=type_, mk_idx=i, k_idx=k_idx, price=price,
                        ts=candles[k_idx]["ts"], known_idx=known_idx,
                        known_at=_event_at(candles, known_idx)))
    return out


def _more_extreme(a, b):
    """Whether same-type endpoint a extends b; equal prices keep the old anchor."""
    return (a["price"] > b["price"] if a["type"] == "top"
            else a["price"] < b["price"])


def _interval_extremes(a, b, merged):
    span = merged[a["mk_idx"]:b["mk_idx"] + 1]
    if not span:
        return False
    top, bot = (a, b) if a["type"] == "top" else (b, a)
    return (max(k["h"] for k in span) == top["price"] and
            min(k["l"] for k in span) == bot["price"])


def _can_form_bi(fx_a, fx_b, merged=None):
    """At least five processed bars, alternating endpoints, and global extremes.

    The optional third argument preserves the former two-argument API; callers
    constructing strokes always supply the processed bars for the range check.
    """
    if fx_a["type"] == fx_b["type"]:
        return False
    if fx_b["mk_idx"] - fx_a["mk_idx"] + 1 < 5:
        return False
    top, bot = ((fx_a, fx_b) if fx_a["type"] == "top"
                else (fx_b, fx_a))
    if top["price"] <= bot["price"]:
        return False
    return merged is None or _interval_extremes(fx_a, fx_b, merged)


def _stroke(a, b, candles, unfinished=False, state="confirmed", reason=None):
    known_idx = max(a["known_idx"], b["known_idx"], b.get("bi_known_idx", 0))
    locked_idx = b.get("locked_idx") if not unfinished else None
    result = dict(
        dir="up" if b["type"] == "top" else "down",
        start_idx=a["k_idx"], end_idx=b["k_idx"],
        start_price=a["price"], end_price=b["price"],
        start_ts=candles[a["k_idx"]]["ts"], end_ts=candles[b["k_idx"]]["ts"],
        start_mk=a["mk_idx"], end_mk=b["mk_idx"],
        mk_count=b["mk_idx"] - a["mk_idx"] + 1,
        unfinished=unfinished, state="locked" if locked_idx is not None else state,
        locked=locked_idx is not None, known_idx=known_idx,
        known_at=_event_at(candles, known_idx), locked_idx=locked_idx,
        locked_at=_event_at(candles, locked_idx) if locked_idx is not None else None,
    )
    if reason:
        result["reason"] = reason
    return result


def build_bi(fenxing, candles, merged=None):
    """Return continuous legal confirmed strokes and an explicit provisional tail.

    Reconstruct the adjustable suffix as the longest legal alternating path to
    its latest reachable confirmed fractal. All short candidates stay in the
    interval extrema and cannot disappear from later validity checks. Prefix
    endpoints lock only after two subsequent strokes and while still being the
    same-type extreme through the latest fractal. Revisions never cross a lock.

    ``state`` is locked / confirmed / candidate / extending / blocked.
    A blocked item describes an observation, and must not be drawn as a stroke.
    ``eps`` contains the selected confirmed fractals. During a raw-price
    extension its last endpoint is retained there, while the last displayed
    stroke is provisional until the extended extreme has a confirmed fractal.
    """
    if merged is None:
        merged = merge_klines(candles)
    eps = []
    locked_endpoint = -1
    blocked_reason = None
    history = []
    first_selected = {}

    for source in fenxing:
        fx = dict(source)
        if fx["mk_idx"] + 1 >= len(merged):
            continue
        fx.setdefault("known_idx", merged[fx["mk_idx"] + 1]["idx_start"])
        fx["known_at"] = _event_at(candles, fx["known_idx"])
        fx.pop("locked_idx", None)
        fx.pop("locked_at", None)
        history.append(fx)
        if locked_endpoint >= 0:
            prefix = eps[:locked_endpoint + 1]
            anchor = prefix[-1]
            nodes = [anchor] + [f for f in history if f["mk_idx"] > anchor["mk_idx"]]
            paths = [[anchor]] + [None] * (len(nodes) - 1)
        else:
            prefix = []
            nodes = history
            paths = [[f] for f in nodes]
        for end in range(1, len(nodes)):
            for start in range(end):
                if paths[start] is None or not _can_form_bi(nodes[start], nodes[end], merged):
                    continue
                path = paths[start] + [nodes[end]]
                if paths[end] is None or len(path) >= len(paths[end]):
                    paths[end] = path
        reachable = [i for i, path in enumerate(paths)
                     if path is not None and (prefix or len(path) >= 2)]
        if reachable:
            # Recency first; longest path resolves alternatives to that endpoint.
            selected = [dict(f) for f in paths[reachable[-1]]]
            eps = prefix[:-1] + selected if prefix else selected
            blocked_reason = None if eps[-1]["mk_idx"] == fx["mk_idx"] else "no_legal_continuation"
        elif not prefix:
            # No stroke yet: retain the first type's most extreme start candidate.
            first_type = history[0]["type"]
            same_type = [f for f in history if f["type"] == first_type]
            choose = max if first_type == "top" else min
            eps = [dict(choose(same_type, key=lambda f: f["price"]))]

        for a, b in zip(eps, eps[1:]):
            edge = (a["k_idx"], b["k_idx"], a["price"], b["price"])
            first_selected.setdefault(edge, fx["known_idx"])
            b["bi_known_idx"] = first_selected[edge]

        # Do not freeze an endpoint already surpassed by its adjustable suffix.
        # Such an endpoint is precisely what short reversal repair may retract.
        new_locked = locked_endpoint
        for index in range(locked_endpoint + 1, len(eps) - 2):
            endpoint = eps[index]
            span = merged[endpoint["mk_idx"]:fx["mk_idx"] + 1]
            extreme = (max(k["h"] for k in span) if endpoint["type"] == "top"
                       else min(k["l"] for k in span))
            if endpoint["price"] == extreme:
                new_locked = index
        if new_locked > locked_endpoint:
            for index in range(locked_endpoint + 1, new_locked + 1):
                eps[index]["locked_idx"] = fx["known_idx"]
                eps[index]["locked_at"] = _event_at(candles, fx["known_idx"])
            locked_endpoint = new_locked

    bis = [_stroke(a, b, candles) for a, b in zip(eps, eps[1:])]
    if not eps or eps[-1]["mk_idx"] + 1 >= len(merged):
        return bis, eps

    last = eps[-1]
    tail = list(enumerate(merged[last["mk_idx"] + 1:], last["mk_idx"] + 1))
    seen_idx = merged[-1]["idx_end"]

    def tail_endpoint(type_):
        key = "h" if type_ == "top" else "l"
        choose = max if type_ == "top" else min
        mk_idx, k = choose(tail, key=lambda item: item[1][key])
        k_idx = k["hi_idx" if type_ == "top" else "lo_idx"]
        return dict(type=type_, mk_idx=mk_idx, k_idx=k_idx, price=k[key],
                    ts=candles[k_idx]["ts"], known_idx=seen_idx,
                    known_at=_event_at(candles, seen_idx))

    extension = tail_endpoint(last["type"])
    if (_more_extreme(extension, last) and len(eps) > 1 and
            not bis[-1]["locked"] and _can_form_bi(eps[-2], extension, merged)):
        bis[-1] = _stroke(eps[-2], extension, candles, True, "extending",
                          "new_extreme_waiting_for_fractal")
        return bis, eps

    candidate = tail_endpoint("bottom" if last["type"] == "top" else "top")
    legal_range = _interval_extremes(last, candidate, merged)
    direction_ok = (candidate["price"] < last["price"] if last["type"] == "top"
                    else candidate["price"] > last["price"])
    if legal_range and direction_ok and not _more_extreme(extension, last):
        reason = ("insufficient_merged_bars" if candidate["mk_idx"] - last["mk_idx"] < 4
                  else "waiting_for_eligible_fractal")
        bis.append(_stroke(last, candidate, candles, True, "candidate", reason))
    else:
        bis.append(_stroke(last, candidate, candles, True, "blocked",
                           blocked_reason or "tail_crosses_confirmed_endpoint"))
    return bis, eps
