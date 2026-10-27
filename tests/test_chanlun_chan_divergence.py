"""Structural divergence contracts, using real center construction and controlled MACD areas."""

import copy
import unittest
from unittest.mock import patch

import pandas as pd

from analysis.chanlun.chan import build_zhongshu, detect_beichi, find_bsp


TREND_POINTS = [140, 120, 135, 122, 134, 121, 130, 100,
                110, 101, 109, 102, 108, 101, 106, 90]
RANGE_POINTS = [140, 120, 135, 122, 134, 121, 133, 119]


def series(points, mirror=False):
    """Four candles per confirmed stroke; mirrored prices exercise the short side."""
    if mirror:
        points = [300 - p for p in points]
    candles = []
    for start, end in zip(points, points[1:]):
        for offset in range(4):
            price = start + (end - start) * offset / 4
            candles.append({"ts": len(candles) * 300000, "o": price, "c": price,
                            "h": price + 0.1, "l": price - 0.1, "vol": 100,
                            "confirm": 1})
    price = points[-1]
    candles.append({"ts": len(candles) * 300000, "o": price, "c": price,
                    "h": price + 0.1, "l": price - 0.1, "vol": 100, "confirm": 1})
    bis = []
    for i, (start, end) in enumerate(zip(points, points[1:])):
        bis.append({"dir": "down" if end < start else "up",
                    "start_idx": i * 4, "end_idx": (i + 1) * 4,
                    "start_price": start, "end_price": end,
                    "start_ts": candles[i * 4]["ts"],
                    "end_ts": candles[(i + 1) * 4]["ts"],
                    "start_mk": i * 4, "end_mk": (i + 1) * 4,
                    "mk_count": 5, "unfinished": False})
    return bis, candles


def signals(points, areas, mirror=False, final_centers=None):
    bis, candles = series(points, mirror)
    hist = [0.0] * len(candles)
    for i, b in enumerate(bis):
        magnitude = areas.get(i, 1.0)
        hist[b["start_idx"] + 2] = -magnitude if b["dir"] == "down" else magnitude
    # Zero endpoint bars keep adjacent strokes' MACD areas independent.
    dif = pd.Series(hist)
    zss = build_zhongshu(bis, candles) if final_centers is None else final_centers
    with patch("analysis.chanlun.chan.macd_hist_list", return_value=hist), \
            patch("analysis.chanlun.chan.macd", return_value=(dif, dif, dif)):
        result = detect_beichi(bis, zss, candles, "5m")
    return result, bis, candles


def signal_at(result, bi_idx):
    return next((signal for signal in result if signal["bi_idx"] == bi_idx), None)


class DivergenceStructureTests(unittest.TestCase):
    def test_trend_compares_each_centers_departure_not_neighboring_strokes(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, _, _ = signals(TREND_POINTS, {6: 10, 12: 1, 14: 2}, mirror)
                signal = signal_at(result, 14)
                self.assertIsNotNone(signal)
                self.assertEqual(signal["kind"], "trend")
                self.assertTrue(signal["struct_ok"])
                self.assertEqual(signal["dir"], "up" if mirror else "down")
                self.assertEqual(signal["area_ratio"], 0.2)
                self.assertEqual((signal["area_in"], signal["area_out"]), (10, 2))
                comparison = signal["comparison"]
                self.assertEqual(comparison["before"]["bi_idx"], 6)
                self.assertEqual(comparison["after"]["bi_idx"], 14)
                self.assertEqual([z["bi_start"] for z in comparison["centers"]], [0, 7])
                self.assertEqual(comparison["as_of_bi"], 14)
                self.assertTrue(all(z["bi_end"] < 14 for z in comparison["centers"]))

    def test_single_formed_center_gives_range_divergence(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, _, _ = signals(RANGE_POINTS, {4: 10, 6: 2}, mirror)
                signal = signal_at(result, 6)
                self.assertEqual(signal["kind"], "panzheng")
                self.assertTrue(signal["struct_ok"])
                self.assertEqual(signal["comparison"]["before"]["bi_idx"], 4)
                center = signal["comparison"]["centers"][0]
                self.assertLess(center["formed_bi"], 4)
                self.assertEqual(center["bi_end"], 5)

    def test_departure_after_nine_stroke_cap_can_enter_second_center(self):
        points = [120, 140, 125, 138, 126, 137, 127, 136, 128, 135,
                  100, 110, 101, 109, 102, 108, 101, 106, 90]
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, _, _ = signals(points, {9: 10, 15: 1, 17: 2}, mirror)
                signal = signal_at(result, 17)
                self.assertEqual(signal["kind"], "trend")
                comparison = signal["comparison"]
                self.assertEqual(comparison["centers"][0]["bi_end"], 9)
                self.assertEqual(comparison["centers"][0]["atomic_bi_count"], 9)
                self.assertEqual(comparison["centers"][1]["bi_start"], 10)
                self.assertEqual(comparison["before"]["bi_idx"], 9)
                self.assertEqual(signal["area_ratio"], 0.2)

    def test_trend_requires_new_extreme_across_the_whole_comparison_interval(self):
        for mirror in (False, True):
            for next_extreme, expected in ((95, False), (90, False), (89, True)):
                with self.subTest(mirror=mirror, next_extreme=next_extreme):
                    result, _, _ = signals(TREND_POINTS + [105, next_extreme],
                                           {6: 10, 14: 3, 16: 1}, mirror)
                    self.assertEqual(signal_at(result, 14)["kind"], "trend")
                    self.assertEqual(signal_at(result, 16) is not None, expected)

    def test_two_overlapping_centers_do_not_make_a_trend(self):
        points = [120, 140, 125, 138, 126, 137, 127, 136, 128, 135,
                  126, 137, 127, 136, 128, 135, 129, 134, 119]
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, bis, candles = signals(points, {9: 10, 15: 10, 17: 2}, mirror)
                self.assertEqual(len(build_zhongshu(bis[:17], candles)), 2)
                signal = signal_at(result, 17)
                self.assertEqual(signal["kind"], "panzheng")
                self.assertEqual(len(signal["comparison"]["centers"]), 1)

    def test_no_center_is_momentum_only_on_both_sides(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, _, _ = signals([140, 120, 130, 110], {0: 10, 2: 2}, mirror)
                signal = signal_at(result, 2)
                self.assertEqual(signal["kind"], "momentum")
                self.assertFalse(signal["struct_ok"])
                self.assertEqual(signal["comparison"]["centers"], [])
                self.assertTrue(signal["reason"])

    def test_comparison_stroke_cannot_form_its_own_prior_center(self):
        result, _, _ = signals([140, 120, 135, 122, 134, 119], {2: 10, 4: 2})
        self.assertEqual(signal_at(result, 4)["kind"], "momentum")

    def test_threshold_is_strict_and_does_not_fallback_to_easier_comparison(self):
        for after, expected in ((6.999, True), (7, False), (10, False), (12, False)):
            with self.subTest(after=after):
                result, _, _ = signals(TREND_POINTS, {6: 10, 12: 100, 14: after})
                self.assertEqual(signal_at(result, 14) is not None, expected)

    def test_new_extreme_and_positive_reference_area_are_required(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, _, _ = signals([140, 120, 130, 120], {0: 10, 2: 2}, mirror)
                self.assertIsNone(signal_at(result, 2))
                result, _, _ = signals([140, 120, 130, 110], {0: 0, 2: 2}, mirror)
                self.assertIsNone(signal_at(result, 2))

    def test_later_extension_does_not_reclassify_or_change_comparison(self):
        for points, areas, index in ((TREND_POINTS, {6: 10, 14: 2}, 14),
                                     (RANGE_POINTS, {4: 10, 6: 2}, 6)):
            with self.subTest(index=index):
                old, _, _ = signals(points, areas)
                extended = points + [points[-1] + 12, points[-1] + 1, points[-1] + 10]
                new, _, _ = signals(extended, areas)
                self.assertEqual(signal_at(old, index), signal_at(new, index))

    def test_future_center_cannot_retroactively_structure_early_momentum(self):
        points = [140, 120, 130, 110]
        old, _, _ = signals(points, {0: 10, 2: 2})
        future, _, _ = signals(points + [129, 111, 128, 109], {0: 10, 2: 2})
        self.assertEqual(signal_at(old, 2), signal_at(future, 2))
        self.assertEqual(signal_at(future, 2)["kind"], "momentum")

    def test_final_zhongshu_argument_does_not_control_historical_classification(self):
        normal, _, _ = signals(TREND_POINTS, {6: 10, 14: 2})
        ignored, _, _ = signals(TREND_POINTS, {6: 10, 14: 2}, final_centers=[])
        self.assertEqual(normal, ignored)

    def test_unfinished_current_stroke_is_excluded(self):
        bis, candles = series([140, 120, 130, 110])
        bis[-1]["unfinished"] = True
        self.assertEqual(detect_beichi(bis, [], candles, "5m"), [])


class FirstAndSecondPointTests(unittest.TestCase):
    def test_only_structured_trend_divergence_creates_first_and_second_points(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                result, bis, candles = signals(TREND_POINTS + [97, 92],
                                               {6: 10, 14: 2}, mirror)
                original = signal_at(result, 14)
                self.assertEqual(original["kind"], "trend")
                expected = ["S1", "S2"] if mirror else ["B1", "B2"]
                for kind, struct_ok, allowed in (("trend", True, True),
                                                ("trend", False, False),
                                                ("panzheng", True, False),
                                                ("momentum", False, False)):
                    with self.subTest(kind=kind, struct_ok=struct_ok):
                        bc = dict(original, kind=kind, struct_ok=struct_ok)
                        points = find_bsp(bis, [], [bc], candles, "5m")
                        first_second = [p["type"] for p in points if p["type"][1] in "12"]
                        self.assertEqual(first_second, expected if allowed else [])
                        # 三类点由独立的离开/回踩结构产生，不受此背驰分类影响。
                        self.assertEqual([p for p in points if p["type"][1] == "3"],
                                         find_bsp(bis, [], [], candles, "5m"))
                missing_structure = copy.deepcopy(original)
                missing_structure.pop("struct_ok")
                self.assertEqual(find_bsp(bis, [], [missing_structure], candles, "5m"),
                                 find_bsp(bis, [], [], candles, "5m"))

    def test_second_point_must_hold_first_point_extreme(self):
        for mirror in (False, True):
            result, bis, candles = signals(TREND_POINTS + [97, 89], {6: 10, 14: 2}, mirror)
            bc = signal_at(result, 14)
            points = find_bsp(bis, [], [bc], candles, "5m")
            self.assertEqual([p["type"] for p in points if p["type"][1] in "12"],
                             ["S1"] if mirror else ["B1"])


if __name__ == "__main__":
    unittest.main()
