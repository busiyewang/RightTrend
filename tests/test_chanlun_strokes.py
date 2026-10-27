"""Stroke geometry and causal locking contracts; optional external market fixtures."""

import json
import os
from pathlib import Path
import unittest

from analysis.chanlun.chan_strokes import build_bi, find_fenxing, merge_klines


def candles(levels):
    return [dict(ts=i * 300000, o=p + .4, c=p + .6, h=p + 1, l=p,
                 vol=100, confirm=1) for i, p in enumerate(levels)]


def mirror(cs):
    return [dict(k, o=100 - k["o"], c=100 - k["c"],
                 h=100 - k["l"], l=100 - k["h"]) for k in cs]


def wave(points, steps=5):
    levels = []
    for a, b in zip(points, points[1:]):
        levels.extend(a + (b - a) * i / steps for i in range(steps))
    levels.append(points[-1])
    return candles(levels)


def analyze(cs):
    merged = merge_klines(cs)
    fractals = find_fenxing(merged, cs)
    bis, eps = build_bi(fractals, cs, merged)
    return merged, fractals, bis, eps


def identity(b):
    return tuple(b[key] for key in ("dir", "start_idx", "end_idx", "start_price",
                                    "end_price", "known_idx", "locked_idx"))


class StrokeTests(unittest.TestCase):
    def assert_valid(self, cs, merged, bis):
        finished = [b for b in bis if not b["unfinished"]]
        for b in finished:
            with self.subTest(start=b["start_idx"], end=b["end_idx"]):
                self.assertGreaterEqual(b["mk_count"], 5)
                span = merged[b["start_mk"]:b["end_mk"] + 1]
                self.assertEqual(max(k["h"] for k in span), max(b["start_price"], b["end_price"]))
                self.assertEqual(min(k["l"] for k in span), min(b["start_price"], b["end_price"]))
                self.assertEqual(cs[b["start_idx"]]["l" if b["dir"] == "up" else "h"], b["start_price"])
                self.assertEqual(cs[b["end_idx"]]["h" if b["dir"] == "up" else "l"], b["end_price"])
                self.assertGreater(b["known_idx"], b["end_idx"])
                self.assertLess(b["known_idx"], len(cs))
                if b["locked"]:
                    self.assertGreater(b["locked_idx"], b["known_idx"])
                    self.assertLess(b["locked_idx"], len(cs))
        for a, b in zip(finished, finished[1:]):
            self.assertNotEqual(a["dir"], b["dir"])
            self.assertEqual((a["end_idx"], a["end_price"]), (b["start_idx"], b["start_price"]))

    def test_inclusion_direction_extreme_anchor_and_first_known_event(self):
        cs = [dict(ts=i * 300000, o=l, c=h, h=h, l=l, confirm=1)
              for i, (h, l) in enumerate([(10, 5), (12, 7), (11, 8), (11, 6), (10, 7)])]
        for series, type_ in ((cs, "top"), (mirror(cs), "bottom")):
            merged, fractals, _, _ = analyze(series)
            self.assertEqual(len(merged), 3)
            self.assertEqual(fractals[0]["type"], type_)
            self.assertEqual(fractals[0]["k_idx"], 1)
            self.assertEqual(fractals[0]["known_idx"], 3)
            self.assertEqual(fractals[0]["known_at"], series[3]["ts"])
            self.assertEqual(find_fenxing(merge_klines(series[:4]), series[:4]), fractals)

    def test_short_extreme_cannot_be_replaced_by_a_weaker_later_fractal(self):
        cs = candles([2, 0, 5, 10, 7, 5, 6, 7, 8, 7, 6])
        for series in (cs, mirror(cs)):
            merged, _, bis, _ = analyze(series)
            self.assert_valid(series, merged, bis)
            self.assertEqual([b for b in bis if not b["unfinished"]], [])
            self.assertEqual(bis[-1]["end_idx"], 3)
            self.assertTrue(bis[-1]["unfinished"])

    def test_start_break_reselects_tail_instead_of_swallowing_lower_low(self):
        cs = candles([6, 5, 6, 7, 8, 9, 10, 4, 6, 8, 10, 12, 11])
        for series in (cs, mirror(cs)):
            merged, _, bis, _ = analyze(series)
            self.assert_valid(series, merged, bis)
            finished = [b for b in bis if not b["unfinished"]]
            self.assertEqual([(b["start_idx"], b["end_idx"]) for b in finished], [(7, 11)])

    def test_raw_new_extreme_extends_previous_stroke_provisionally(self):
        cs = candles([6, 5, 6, 7, 8, 9, 10, 9, 10, 11, 12])
        for series, direction in ((cs, "up"), (mirror(cs), "down")):
            merged, _, bis, _ = analyze(series)
            self.assertEqual(len(bis), 1)
            extension = bis[-1]
            self.assertEqual((extension["state"], extension["dir"]), ("extending", direction))
            self.assertEqual((extension["start_idx"], extension["end_idx"]), (1, 10))
            self.assertTrue(extension["unfinished"])
            self.assertFalse(extension["locked"])
            span = merged[extension["start_mk"]:extension["end_mk"] + 1]
            self.assertEqual(max(k["h"] for k in span), max(extension["start_price"], extension["end_price"]))
            self.assertEqual(min(k["l"] for k in span), min(extension["start_price"], extension["end_price"]))

    def test_mirror_has_identical_geometry_and_confirmation_events(self):
        cs = wave([14, 5, 20, 9, 25, 12, 28, 15, 30, 14, 22, 8, 25])
        original = analyze(cs)[2]
        reflected = analyze(mirror(cs))[2]
        self.assertEqual(len(original), len(reflected))
        for a, b in zip(original, reflected):
            self.assertNotEqual(a["dir"], b["dir"])
            for key in ("start_idx", "end_idx", "known_idx", "locked_idx", "state", "locked"):
                self.assertEqual(a[key], b[key])
            self.assertEqual(a["start_price"], 100 - b["start_price"])
            self.assertEqual(a["end_price"], 100 - b["end_price"])

    def test_locked_prefix_never_retracts_as_closed_candles_arrive(self):
        cs = wave([14, 5, 20, 9, 25, 12, 28, 15, 30, 14, 22, 8, 25, 3, 27])
        for series in (cs, mirror(cs)):
            locked = set()
            for count in range(3, len(series) + 1):
                merged, _, bis, _ = analyze(series[:count])
                self.assert_valid(series[:count], merged, bis)
                now = {identity(b) for b in bis if b["locked"]}
                self.assertTrue(locked.issubset(now))
                locked = now
            self.assertGreater(len(locked), 4)

    def test_unclosed_candle_cannot_confirm_or_extend_a_stroke(self):
        cs = candles([6, 5, 6, 7, 8, 9, 10, 9])
        unclosed = dict(ts=len(cs) * 300000, o=50, c=50, h=90, l=0, confirm=0)
        self.assertEqual(analyze(cs)[2], analyze(cs + [unclosed])[2])


@unittest.skipUnless(os.environ.get("CHAN_AUDIT_FIXTURES"),
                     "Set CHAN_AUDIT_FIXTURES to external market JSON snapshots")
class MarketStrokeTests(unittest.TestCase):
    assert_valid = StrokeTests.assert_valid

    def test_external_market_geometry_coverage_and_locked_prefixes(self):
        root = Path(os.environ["CHAN_AUDIT_FIXTURES"])
        names = ["ETH-USDT-SWAP_5m.json", "ETH-USDT-SWAP_1H.json",
                 "ETH-USDT-SWAP_4H.json", "BTC-USDT-SWAP_5m.json"]
        for name in names:
            with self.subTest(market=name):
                cs = [c for c in json.loads((root / name).read_text())["candles"] if c["confirm"]]
                merged, _, bis, _ = analyze(cs)
                self.assert_valid(cs, merged, bis)
                finished = [b for b in bis if not b["unfinished"]]
                self.assertGreaterEqual(len(finished), 30)
                self.assertGreaterEqual(sum(b["locked"] for b in bis), 25)
                self.assertGreaterEqual(finished[-1]["end_idx"], len(cs) - 12)
                self.assertNotEqual(bis[-1]["state"], "blocked")
                locked = set()
                for count in list(range(70, len(cs), 37)) + [len(cs)]:
                    prefix = cs[:count]
                    merged, _, bis, _ = analyze(prefix)
                    self.assert_valid(prefix, merged, bis)
                    now = {identity(b) for b in bis if b["locked"]}
                    self.assertTrue(locked.issubset(now), (name, count))
                    locked = now


if __name__ == "__main__":
    unittest.main()
