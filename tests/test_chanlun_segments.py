"""Independent geometric fixtures for the two characteristic-sequence cases."""

import copy
import random
import unittest

from analysis.chanlun.chan_segments import build_segments


def strokes(points, mirror=False):
    """Each observation represents a newly locked pen, not its earlier extreme.

    Endpoints, first-observed events and lock events are deliberately distinct
    so a timestamp copied from an extreme cannot pass the timing assertions.
    """
    if mirror:
        points = [100 - x for x in points]
    out = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        out.append({"dir": "up" if b > a else "down",
                    "start_idx": i * 5, "end_idx": (i + 1) * 5,
                    "start_ts": i * 5000, "end_ts": (i + 1) * 5000,
                    "start_price": a, "end_price": b,
                    "known_idx": (i + 1) * 5 + 1,
                    "known_at": (i + 1) * 5000 + 1000,
                    "locked_idx": (i + 1) * 5 + 6,
                    "locked_at": (i + 1) * 5000 + 6000,
                    "unfinished": False, "locked": True})
    return out


def confirmed(items):
    return [s for s in items if s["locked"]]


class SegmentTests(unittest.TestCase):
    def test_three_strokes_seed_but_do_not_confirm_a_segment(self):
        self.assertEqual(build_segments(strokes([0, 10, 5])), [])
        items = build_segments(strokes([0, 10, 5, 12]))
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["bi_count"], items[0]["state"], items[0]["locked"]),
                         (3, "candidate", False))

    def test_three_strokes_without_initial_overlap_are_not_a_segment(self):
        self.assertEqual(build_segments(strokes([0, 2, -2, -1])), [])
        self.assertEqual(build_segments(strokes([0, 2, -2, 0])), [])

    def test_no_gap_requires_the_third_characteristic_element(self):
        bis = strokes([0, 10, 5, 12, 7, 11, 4])
        self.assertEqual(confirmed(build_segments(bis[:5])), [])
        result = confirmed(build_segments(bis))
        self.assertEqual(len(result), 1)
        segment = result[0]
        self.assertEqual((segment["dir"], segment["bi_start"], segment["bi_end"]), ("up", 0, 2))
        self.assertFalse(segment["confirmation"]["gap"])
        self.assertEqual(segment["confirmation"]["proof_bi"], 5)
        self.assertEqual(segment["end_idx"], 15)
        self.assertEqual(segment["known_idx"], bis[5]["locked_idx"])
        self.assertEqual(segment["known_at"], bis[5]["locked_at"])
        self.assertGreater(segment["known_at"], segment["end_ts"])

    def test_touching_features_have_no_gap(self):
        segment = confirmed(build_segments(strokes([0, 10, 5, 15, 10, 13, 9])))[0]
        self.assertFalse(segment["confirmation"]["gap"])

    def test_left_characteristic_inclusion_preserves_source_strokes(self):
        # Down features [5,10] and [6,9] combine to [6,10].
        segment = confirmed(build_segments(strokes([0, 10, 5, 9, 6, 12, 8, 11, 4])))[0]
        self.assertEqual(segment["bi_end"], 4)
        left = segment["confirmation"]["primary_features"][0]
        self.assertEqual(left, {"h": 10, "l": 6, "bis": [1, 3]})

    def test_lesson_71_strong_reversal_is_not_swallowed_across_boundary(self):
        # [3,12] contains pre-boundary [5,10].  The following down stroke
        # reaches 2, so the 12 pivot must survive inclusion processing.
        segment = confirmed(build_segments(strokes([0, 10, 5, 12, 3, 8, 2])))[0]
        self.assertEqual((segment["end_price"], segment["bi_end"]), (12, 2))
        self.assertTrue(segment["confirmation"]["boundary_inclusion"])
        self.assertFalse(segment["confirmation"]["gap"])

    def test_lesson_71_inside_third_stroke_waits_for_direction(self):
        inside = strokes([0, 10, 5, 12, 3, 8, 4])
        self.assertEqual(confirmed(build_segments(inside)), [])
        # 3.5 is below the last inside stroke's low (4), but still above the
        # first reversal's low (3): that is still not a directional break.
        self.assertEqual(confirmed(build_segments(strokes([0, 10, 5, 12, 3, 8, 4, 6, 3.5]))), [])
        segment = confirmed(build_segments(strokes([0, 10, 5, 12, 3, 8, 4, 6, 2])))[0]
        self.assertEqual(segment["confirmation"]["proof_bi"], 7)
        self.assertEqual(segment["confirmation"]["primary_features"][1]["bis"], [3, 5])

    def test_gap_waits_for_reverse_fractal_even_after_primary_top(self):
        bis = strokes([0, 10, 5, 15, 12, 14, 11, 13, 12, 14])
        pending = build_segments(bis[:6])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending_gap")
        self.assertEqual(pending[0]["confirmation"]["primary_bi"], 5)
        self.assertIsNone(pending[0]["confirmation"]["reverse_bi"])
        self.assertEqual(confirmed(build_segments(bis[:8])), [])
        result = confirmed(build_segments(bis))
        first = result[0]
        self.assertEqual(first["bi_end"], 2)
        self.assertTrue(first["confirmation"]["gap"])
        self.assertEqual(first["confirmation"]["reverse_bi"], 8)
        self.assertEqual(first["known_idx"], bis[8]["locked_idx"])
        # The original gap starts at 10; reversal low 11 has not closed it.
        self.assertGreater(min(b["end_price"] for b in bis[3:]), 10)
        self.assertEqual([x["dir"] for x in result], ["up", "down"])

    def test_lesson_78_reverse_sequence_uses_full_inclusion(self):
        # Reverse up-features [10,13] and [11,12] must merge down to [10,12].
        # Only the subsequent [10.5,14] produces the reverse bottom fractal.
        bis = strokes([0, 10, 5, 15, 12, 14, 10, 13, 11, 12, 10.5, 14])
        self.assertEqual(confirmed(build_segments(bis[:10])), [])
        first = confirmed(build_segments(bis))[0]
        self.assertTrue(first["confirmation"]["gap"])
        self.assertEqual(first["confirmation"]["reverse_bi"], 10)
        middle = first["confirmation"]["reverse_features"][1]
        self.assertEqual(middle, {"h": 12, "l": 10, "bis": [6, 8]})

    def test_lesson_78_new_extreme_cancels_unconfirmed_gap(self):
        prefix = strokes([0, 10, 5, 15, 12, 14, 11, 16])
        result = build_segments(prefix)
        self.assertEqual(confirmed(result), [])
        self.assertEqual((result[-1]["end_price"], result[-1]["state"]), (16, "candidate"))
        later = confirmed(build_segments(strokes([0, 10, 5, 15, 12, 14, 11, 16, 13, 15, 10])))
        self.assertEqual(later[0]["bi_end"], 6)
        self.assertFalse(any(s["end_price"] == 15 for s in later))

    def test_both_directions_are_exact_mirrors(self):
        examples = [[0, 10, 5, 12, 7, 11, 4],
                    [0, 10, 5, 12, 3, 8, 4, 6, 2],
                    [0, 10, 5, 15, 12, 14, 11, 13, 12, 14],
                    [0, 10, 5, 15, 12, 14, 10, 13, 11, 12, 10.5, 14]]
        for points in examples:
            with self.subTest(points=points):
                up = build_segments(strokes(points))
                down = build_segments(strokes(points, mirror=True))
                self.assertEqual(len(up), len(down))
                for a, b in zip(up, down):
                    self.assertNotEqual(a["dir"], b["dir"])
                    self.assertEqual(100 - a["end_price"], b["end_price"])
                    for key in ("bi_start", "bi_end", "state", "known_idx", "locked"):
                        self.assertEqual(a[key], b[key])
                    self.assertEqual(a["confirmation"]["gap"], b["confirmation"]["gap"])

    def test_unlocked_or_unfinished_evidence_cannot_confirm(self):
        for change in ({"locked": False}, {"unfinished": True}):
            with self.subTest(change=change):
                bis = strokes([0, 10, 5, 12, 7, 11, 4])
                bis[-1].update(change)
                result = build_segments(bis)
                self.assertEqual(confirmed(result), [])
                self.assertEqual(result[-1]["state"], "candidate")
                self.assertTrue(result[-1]["confirmation"]["uses_unlocked"])
        bis = strokes([0, 10, 5, 12, 7, 11, 4])
        for b in bis:
            b.pop("locked")
        self.assertEqual(confirmed(build_segments(bis)), [])

    def test_missing_timing_is_unknown_instead_of_extreme_time(self):
        bis = strokes([0, 10, 5, 12, 7, 11, 4])
        bis[2].pop("locked_at")
        bis[2].pop("locked_idx")
        segment = confirmed(build_segments(bis))[0]
        self.assertIsNone(segment["known_at"])
        self.assertIsNone(segment["known_idx"])

    def test_blocked_observation_does_not_supply_a_candidate_feature(self):
        bis = strokes([0, 10, 5, 12, 7, 11, 4])
        bis[-1].update(state="blocked", unfinished=True, locked=False)
        self.assertEqual(build_segments(bis), build_segments(bis[:-1]))
        self.assertEqual(confirmed(build_segments(bis)), [])
        # It also cannot be the third pen that would seed the first segment.
        seed = strokes([0, 10, 5, 12])
        seed[-1].update(state="blocked", unfinished=True, locked=False)
        self.assertEqual(build_segments(seed), [])

    def test_input_is_not_mutated(self):
        bis = strokes([0, 10, 5, 12, 3, 8, 4, 6, 2])
        before = copy.deepcopy(bis)
        build_segments(bis)
        self.assertEqual(bis, before)

    def test_locked_segments_stay_identical_for_every_future_prefix(self):
        rng = random.Random(20260922)
        points = [100]
        for i in range(90):
            points.append(points[-1] + (1 if i % 2 == 0 else -1) * rng.randint(2, 18))
        bis = strokes(points)
        previous = []
        for n in range(3, len(bis) + 1):
            result = build_segments(bis[:n])
            fixed = confirmed(result)
            self.assertEqual(previous, fixed[:len(previous)], "repaint at pen %s" % n)
            for i, segment in enumerate(fixed):
                self.assertGreaterEqual(segment["bi_count"], 3)
                self.assertEqual(segment["bi_count"] % 2, 1)
                self.assertFalse(segment["confirmation"]["uses_unlocked"])
                if i:
                    self.assertEqual(segment["start_idx"], fixed[i - 1]["end_idx"])
                    self.assertGreaterEqual(segment["known_idx"], fixed[i - 1]["known_idx"])
            self.assertLessEqual(len(result) - len(fixed), 1)
            previous = fixed
        self.assertGreaterEqual(len(previous), 5)


if __name__ == "__main__":
    unittest.main()
