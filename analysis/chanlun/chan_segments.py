"""Feature-sequence segments, independent of the application's stroke centres.

Profile: ``feature-sequence-v1``.  The local manual §1.3 and the author's
lessons 67, 71 and 78 define the two termination cases used here.  Original
text archives (the original Sina page was unavailable when checked):
https://chzhshch.org/2007/08/01/0614-教你炒股票67/
https://chzhshch.org/2007/08/16/0639-教你炒股票71/
https://chzhshch.org/2007/09/06/0677-教你炒股票78/

Up segments examine down-stroke features; down segments examine up-stroke
features.  Inclusion is resolved within each sequence.  A hypothesised
boundary protects the preceding feature from being swallowed by the first
reversal stroke (lesson 71).  On its right, an inside third stroke must wait
for a directional break; it does not independently confirm a segment.
A gap requires a fractal in the reverse characteristic sequence, with full
inclusion processing, and is cancelled if the old direction makes a new
extreme before that confirmation (lesson 78).

Only the contiguous, explicitly locked stroke prefix may confirm segments.
Consequently confirmation is later than a movable pen endpoint.  ``known``
and ``locked`` indices refer to the last required stroke-lock event, never to
the segment's historical extreme.  Missing event metadata stays unknown.
The first segment is seeded at the first *observable* directional triple;
the unavailable history before the input window is not reconstructed.
"""

from copy import deepcopy


PROFILE = "feature-sequence-v1"


def _opposite(direction):
    return "down" if direction == "up" else "up"


def _high(b):
    return max(b["start_price"], b["end_price"])


def _low(b):
    return min(b["start_price"], b["end_price"])


def _advances(value, previous, direction):
    return value > previous if direction == "up" else value < previous


def _feature(b, index):
    return {"h": _high(b), "l": _low(b), "bis": [index]}


def _contains(a, b):
    return ((a["h"] >= b["h"] and a["l"] <= b["l"])
            or (b["h"] >= a["h"] and b["l"] <= a["l"]))


def _push_feature(sequence, feature, initial_direction):
    """Incrementally standardise one characteristic sequence, without mutation
    of its input.  The latest non-inclusion movement sets merge direction;
    initial_direction handles inclusion before any such movement exists.
    """
    item = deepcopy(feature)
    if not sequence:
        sequence.append(item)
        return
    last = sequence[-1]
    if not _contains(last, item):
        sequence.append(item)
        return
    direction = initial_direction
    if len(sequence) > 1:
        direction = "up" if last["h"] > sequence[-2]["h"] else "down"
    choose = max if direction == "up" else min
    sequence[-1] = {"h": choose(last["h"], item["h"]),
                    "l": choose(last["l"], item["l"]),
                    "bis": last["bis"] + item["bis"]}


def _standard_features(bis, start, stop, feature_direction, initial_direction):
    sequence = []
    for i in range(start, stop):
        if bis[i]["dir"] == feature_direction:
            _push_feature(sequence, _feature(bis[i], i), initial_direction)
    return sequence


def _fractal(sequence, direction):
    """Strict top for an up segment, strict bottom for a down segment."""
    if len(sequence) < 3:
        return False
    a, b, c = sequence[-3:]
    return all(_advances(b[key], x[key], direction)
               for key in ("h", "l") for x in (a, c))


def _seed_end(bis, start, stop):
    """Three initial overlapping strokes and a directional third/later stroke.

    If the third stroke remains inside the first, direction is not established
    yet (lesson 71).  Later continuation may establish it without discarding
    the original anchor.  Point contact alone is not a positive-width overlap.
    """
    if start + 2 >= stop:
        return None
    first = bis[start]
    triple = bis[start:start + 3]
    if (triple[1]["dir"] == first["dir"]
            or triple[2]["dir"] != first["dir"]
            or min(_high(b) for b in triple) <= max(_low(b) for b in triple)):
        return None
    for j in range(start + 2, stop, 2):
        if (bis[j]["dir"] == first["dir"]
                and _advances(bis[j]["end_price"], first["end_price"], first["dir"])):
            return j
    return None


def _initial_start(bis, stop):
    # Select by observation time, not by which old anchor happens to work after
    # seeing the full future.  This choice is stable when a prefix is extended.
    choices = []
    for start in range(max(0, stop - 2)):
        seed = _seed_end(bis, start, stop)
        if seed is not None:
            choices.append((seed, start))
    return min(choices)[1] if choices else None


def _turn_candidate(bis, start, endpoint, stop):
    direction = bis[start]["dir"]
    reverse = _opposite(direction)
    left = _standard_features(bis, start, endpoint + 1, reverse, direction)
    if not left or endpoint + 1 >= stop:
        return None
    a = left[-1]
    b = _feature(bis[endpoint + 1], endpoint + 1)
    peak_key = "h" if direction == "up" else "l"
    if not _advances(b[peak_key], a[peak_key], direction):
        return None
    # Never merge a and b across the hypothesised boundary.  b may contain a
    # in a strong reversal; that case must retain the reversal's full range.
    gap = b["l"] > a["h"] if direction == "up" else b["h"] < a["l"]
    result = {"endpoint": endpoint, "gap": gap, "proof_bi": None,
              "primary_bi": None, "primary_features": None,
              "reverse_bi": None, "reverse_features": None,
              "boundary_inclusion": _contains(a, b)}
    right = []
    reverse_sequence = []
    price = bis[endpoint]["end_price"]
    for q in range(endpoint + 1, stop):
        stroke = bis[q]
        if stroke["dir"] == direction:
            if _advances(stroke["end_price"], price, direction):
                # No reverse confirmation happened before the renewed extreme.
                return None
            _push_feature(reverse_sequence, _feature(stroke, q), reverse)
            if result["reverse_bi"] is None and _fractal(reverse_sequence, reverse):
                result["reverse_bi"] = q
                result["reverse_features"] = deepcopy(reverse_sequence[-3:])
        else:
            # The suffix is on the reversal side of the boundary.  Inclusion
            # retains its initial low (up -> down) / high (down -> up), so an
            # inside third stroke cannot masquerade as directional destruction.
            _push_feature(right, _feature(stroke, q), reverse)
            if result["primary_bi"] is None and len(right) >= 2:
                r0, r1 = right[:2]
                if all(_advances(r1[key], r0[key], reverse) for key in ("h", "l")):
                    result["primary_bi"] = q
                    result["primary_features"] = deepcopy([a, r0, r1])
        if result["primary_bi"] is not None:
            if not gap or result["reverse_bi"] is not None:
                result["proof_bi"] = q
                return result
    return result if result["primary_bi"] is not None else None


def _find_turn(bis, start, stop):
    seed = _seed_end(bis, start, stop)
    if seed is None:
        return None, None
    complete = None
    pending = []
    for endpoint in range(seed, stop - 1, 2):
        if complete is not None and endpoint + 3 > complete["proof_bi"]:
            break
        search_stop = min(stop, complete["proof_bi"] + 1) if complete else stop
        candidate = _turn_candidate(bis, start, endpoint, search_stop)
        if candidate is None:
            continue
        if candidate["proof_bi"] is not None:
            if complete is None or (candidate["proof_bi"], candidate["endpoint"]) < (complete["proof_bi"], complete["endpoint"]):
                complete = candidate
        else:
            pending.append(candidate)
    waiting = min(pending, key=lambda x: (x["primary_bi"], x["endpoint"])) if pending else None
    return complete, waiting


def _event(bis, start, proof, locked):
    """A missing event cannot safely be replaced with an extreme timestamp."""
    idx_key, at_key = ("locked_idx", "locked_at") if locked else ("known_idx", "known_at")
    evidence = bis[start:proof + 1]
    indices = [b.get(idx_key) for b in evidence]
    times = [b.get(at_key) for b in evidence]
    return (max(indices) if indices and all(x is not None for x in indices) else None,
            max(times) if times and all(x is not None for x in times) else None)


def _segment(bis, start, endpoint, locked, state, turn=None):
    a, b = bis[start], bis[endpoint]
    proof = (turn.get("proof_bi") or turn.get("primary_bi")) if turn else endpoint
    known_idx, known_at = _event(bis, start, proof, locked)
    confirmation = {"method": "feature_sequence", "gap": None,
                    "proof_bi": None, "primary_bi": None, "reverse_bi": None,
                    "primary_features": [], "reverse_features": [],
                    "boundary_inclusion": False}
    if turn:
        confirmation.update({k: deepcopy(v) for k, v in turn.items() if k != "endpoint"})
    confirmation["uses_unlocked"] = any(not x.get("locked") or x.get("unfinished")
                                         for x in bis[start:proof + 1])
    return {"dir": a["dir"], "start_idx": a["start_idx"], "end_idx": b["end_idx"],
            "start_ts": a["start_ts"], "end_ts": b["end_ts"],
            "start_price": a["start_price"], "end_price": b["end_price"],
            "bi_start": start, "bi_end": endpoint, "bi_count": endpoint - start + 1,
            "locked": locked, "unfinished": not locked, "state": state,
            "known_idx": known_idx, "known_at": known_at,
            "locked_idx": known_idx if locked else None,
            "locked_at": known_at if locked else None,
            "profile": PROFILE, "confirmation": confirmation}


def _append(out, segment):
    # A segment whose *start* is not known until a preceding gap is resolved
    # cannot claim an earlier observation time just because its own fractal is
    # already visible retrospectively.
    if out:
        previous = out[-1]
        segment["confirmation"]["start_locked_idx"] = previous["locked_idx"]
        for suffix in ("idx", "at"):
            key = "known_" + suffix
            inherited = previous["locked_" + suffix]
            segment[key] = max(segment[key], inherited) if segment[key] is not None and inherited is not None else None
            if segment["locked"]:
                segment["locked_" + suffix] = segment[key]
    out.append(segment)


def build_segments(bis, candles=None):
    """Return independent ``xianduan`` records, without mutating any input.

    The optional candles argument keeps the same interface as stroke-centre
    construction; it is deliberately not used to guess lock times.  Upstream
    should provide actual ``locked_idx/locked_at`` events on each locked pen.
    A missing ``locked`` flag is unknown, never permission to confirm a segment.
    ``confirmed`` records form an immutable prefix; at most one final
    ``candidate`` or ``pending_gap`` record is available for dashed rendering.
    """
    # A blocked tail is an observation that failed pen formation, not even a
    # provisional pen.  Do not let it supply a feature or initialise a segment.
    blocked = next((i for i, b in enumerate(bis) if b.get("state") == "blocked"), len(bis))
    bis = bis[:blocked]
    if len(bis) < 3:
        return []
    stable_stop = 0
    for b in bis:
        if b.get("locked") is not True or b.get("unfinished", False):
            break
        stable_stop += 1
    start = _initial_start(bis, stable_stop)
    out = []
    if start is not None:
        while start + 2 < stable_stop:
            turn, _ = _find_turn(bis, start, stable_stop)
            if turn is None:
                break
            _append(out, _segment(bis, start, turn["endpoint"], True, "confirmed", turn))
            start = turn["endpoint"] + 1
    else:
        start = _initial_start(bis, len(bis))
    if start is None:
        return out
    seed = _seed_end(bis, start, len(bis))
    if seed is None:
        return out
    turn, pending = _find_turn(bis, start, len(bis))
    observation = turn or pending
    if observation:
        endpoint = observation["endpoint"]
        state = "pending_gap" if observation["proof_bi"] is None else "candidate"
    else:
        direction = bis[start]["dir"]
        choices = range(seed, len(bis), 2)
        endpoint = max(choices, key=lambda i: bis[i]["end_price"]
                       if direction == "up" else -bis[i]["end_price"])
        state = "candidate"
    _append(out, _segment(bis, start, endpoint, False, state, observation))
    return out
