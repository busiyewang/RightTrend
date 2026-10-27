"""中枢生命周期和三类点的位置、时序契约。"""

import copy
import unittest

from analysis.chanlun.chan_centers import build_zhongshu, logical_centers, third_point_candidates


def fixture(points, mirror=False, metadata=False):
    if mirror:
        points = [300 - p for p in points]
    candles = []
    for a, b in zip(points, points[1:]):
        for step in range(4):
            p = a + (b - a) * step / 4
            candles.append({"ts": len(candles) * 300000, "o": p, "c": p,
                            "h": p + 0.1, "l": p - 0.1, "vol": 100})
    p = points[-1]
    candles.append({"ts": len(candles) * 300000, "o": p, "c": p,
                    "h": p + 0.1, "l": p - 0.1, "vol": 100})
    bis = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        row = {"dir": "up" if b > a else "down", "start_idx": i * 4,
               "end_idx": (i + 1) * 4, "start_price": a, "end_price": b,
               "start_ts": candles[i * 4]["ts"], "end_ts": candles[(i + 1) * 4]["ts"],
               "unfinished": False}
        if metadata:
            row.update(known_idx=min(row["end_idx"] + 1, len(candles) - 1), locked=True)
        bis.append(row)
    return bis, candles


class CenterLifecycleTests(unittest.TestCase):
    def test_first_three_set_bounds_and_extreme_times_are_not_confirmation_times(self):
        bis, candles = fixture([100, 110, 103, 109, 104], metadata=True)
        z = build_zhongshu(bis, candles)[0]
        self.assertEqual((z["zd"], z["zg"]), (103, 109))
        self.assertEqual(z["formed_extreme_ts"], candles[12]["ts"])
        self.assertEqual(z["known_idx"], 13)
        self.assertEqual(z["known_at"], candles[13]["ts"])
        self.assertGreater(z["known_at"], z["formed_extreme_ts"])

    def test_ninth_tenth_and_twelfth_strokes_remain_one_logical_center(self):
        points = [100, 110, 103, 109, 104, 108, 105, 107, 106, 108, 105, 109, 104]
        for count in (9, 10, 11, 12):
            with self.subTest(count=count):
                bis, candles = fixture(points[:count + 1])
                atoms = build_zhongshu(bis, candles)
                groups = logical_centers(atoms)
                self.assertEqual(len(groups), 1)
                self.assertTrue(all(z["bi_count"] <= 9 for z in atoms))
                self.assertEqual((groups[0]["zd"], groups[0]["zg"]), (103, 109))
                self.assertEqual(groups[0]["bi_count"], count)
                self.assertEqual(groups[0]["bi_end"], count - 1)
                self.assertTrue(groups[0]["extending"])
                self.assertEqual(groups[0]["extension_pending"], count in (10, 11))
                if count == 12:
                    self.assertEqual(len(atoms), 2)
                    self.assertEqual(atoms[1]["continuation_of"], atoms[0]["group_id"])

    def test_departure_then_outside_return_ends_old_group_and_allows_new_center(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 115, 112, 117, 113])
        groups = logical_centers(build_zhongshu(bis, candles))
        self.assertEqual(len(groups), 2)
        self.assertFalse(groups[0]["extending"])
        self.assertEqual(groups[0]["bi_end"], 4)
        self.assertGreater(groups[1]["zd"], groups[0]["zg"])

    def test_unlocked_stroke_makes_center_provisional_and_unfinished_is_excluded(self):
        bis, candles = fixture([100, 110, 103, 109], metadata=True)
        bis[-1]["locked"] = False
        z = logical_centers(build_zhongshu(bis, candles))[0]
        self.assertFalse(z["locked"])
        self.assertEqual(z["state"], "provisional")
        self.assertIsNone(z["locked_idx"])
        self.assertIsNone(z["locked_at"])
        bis[-1]["unfinished"] = True
        self.assertEqual(build_zhongshu(bis, candles), [])

    def test_formation_span_and_lock_events_are_separate(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 108, 105], metadata=True)
        for i, b in enumerate(bis):
            b["locked_idx"] = min(b["end_idx"] + 3, len(candles) - 1)
        atom = build_zhongshu(bis[:5], candles)[0]
        logical = logical_centers([atom])[0]
        for z in (atom, logical):
            self.assertEqual(z["known_idx"], 13)
            self.assertEqual(z["span_known_idx"], 21)
            self.assertEqual(z["locked_idx"], 23)
            self.assertEqual(z["span_known_at"], candles[21]["ts"])
            self.assertEqual(z["locked_at"], candles[23]["ts"])

    def test_prefix_center_is_unchanged_by_unread_future_strokes(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 108, 105, 107, 106, 108,
                                105, 109, 104, 115, 112, 118])
        before = build_zhongshu(bis[:10], candles[:41])
        copy_before = copy.deepcopy(before)
        build_zhongshu(bis, candles)
        self.assertEqual(before, copy_before)
        self.assertEqual(before, build_zhongshu(bis[:10], candles))


class ThirdPointTests(unittest.TestCase):
    def test_real_departure_and_first_outside_return_on_both_sides(self):
        for mirror, expected in ((False, "B3"), (True, "S3")):
            with self.subTest(mirror=mirror):
                bis, candles = fixture([100, 110, 103, 109, 104, 115, 112], mirror)
                points = third_point_candidates(bis, candles)
                self.assertEqual([p["type"] for p in points], [expected])
                self.assertEqual(points[0]["source_bi_idx"], 5)
                self.assertEqual(points[0]["leave_bi_idx"], 4)
                self.assertEqual(points[0]["source_zs"]["bi_end"], 3)
                self.assertEqual(points[0]["price"], 188 if mirror else 112)

    def test_return_inside_or_exactly_on_boundary_is_not_third_point(self):
        for mirror in (False, True):
            for end in (109, 108):
                bis, candles = fixture([100, 110, 103, 109, 104, 115, end], mirror)
                self.assertEqual(third_point_candidates(bis, candles), [])

    def test_departure_cannot_supply_its_own_prior_center(self):
        bis, candles = fixture([100, 110, 103, 115, 112])
        self.assertEqual(third_point_candidates(bis, candles), [])

    def test_nine_stroke_continuation_still_finds_departure_without_duplicate(self):
        points = [100, 110, 103, 109, 104, 108, 105, 107, 106, 108,
                  105, 109, 104, 115, 112]
        for mirror in (False, True):
            bis, candles = fixture(points, mirror)
            signals = third_point_candidates(bis, candles)
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["leave_bi_idx"], 12)
            self.assertEqual(signals[0]["source_zs"]["bi_count"], 12)

    def test_later_extension_does_not_rewrite_old_candidate_or_repeat_old_center(self):
        base = [100, 110, 103, 109, 104, 115, 112]
        old_bis, old_candles = fixture(base)
        old = third_point_candidates(old_bis, old_candles)
        new_bis, new_candles = fixture(base + [118, 110, 120, 116, 123, 119])
        new = third_point_candidates(new_bis, new_candles)
        original = [p for p in new if p["k_idx"] == old[0]["k_idx"]]
        self.assertEqual(original, old)
        self.assertEqual(sum(p["source_zs"]["group_id"] == 0 for p in new), 1)

    def test_unfinished_return_excluded_and_latest_return_is_provisional(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 115, 112], metadata=True)
        bis[-1]["locked"] = False
        signal = third_point_candidates(bis, candles)[0]
        self.assertFalse(signal["locked"])
        self.assertIsNone(signal["locked_idx"])
        self.assertEqual(signal["known_idx"], bis[-1]["known_idx"])
        bis[-1]["unfinished"] = True
        self.assertEqual(third_point_candidates(bis, candles), [])

    def test_signal_lock_event_includes_return_lock(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 115, 112, 119], metadata=True)
        for b in bis:
            b["locked_idx"] = min(b["end_idx"] + 3, len(candles) - 1)
        signal = third_point_candidates(bis, candles)[0]
        self.assertEqual(signal["known_idx"], 25)
        self.assertEqual(signal["locked_idx"], 27)
        self.assertEqual(signal["locked_at"], candles[27]["ts"])

    def test_center_unknown_at_candidate_time_is_excluded(self):
        bis, candles = fixture([100, 110, 103, 109, 104, 115, 112], metadata=True)
        bis[2]["known_idx"] = len(candles) + 100
        self.assertEqual(third_point_candidates(bis, candles), [])


if __name__ == "__main__":
    unittest.main()
