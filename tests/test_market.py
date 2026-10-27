import unittest

from market.cache import CandleCache
from market.service import MarketService, check_quality
from market.timeframes import DURATION_MS, HOUR_MS, expected_last_closed_open, period_open
from tests.helpers import NOW, FakeOkx, market_data, series, settings


def c(ts, close, confirm=1):
    return {"ts": ts, "o": close, "h": close + 1, "l": close - 1, "c": close, "vol": 1, "confirm": confirm}


class CacheTest(unittest.TestCase):
    def test_overlap_out_of_order_and_duplicates_merge_sorted(self):
        cache = CandleCache(100)
        cache.merge("X", "1H", [c(3 * HOUR_MS, 3), c(1 * HOUR_MS, 1), c(2 * HOUR_MS, 2)])
        cache.merge("X", "1H", [c(2 * HOUR_MS, 2), c(4 * HOUR_MS, 4), c(3 * HOUR_MS, 3), c(4 * HOUR_MS, 4)])
        snap = cache.snapshot("X", "1H")
        self.assertEqual([x["ts"] for x in snap], [HOUR_MS * i for i in (1, 2, 3, 4)])

    def test_forming_bar_becomes_confirmed_and_is_not_reverted(self):
        cache = CandleCache(100)
        cache.merge("X", "1H", [c(HOUR_MS, 10, confirm=0)])
        cache.merge("X", "1H", [c(HOUR_MS, 11, confirm=1)])
        self.assertEqual(cache.snapshot("X", "1H")[0]["c"], 11)
        self.assertEqual(cache.snapshot("X", "1H")[0]["confirm"], 1)
        cache.merge("X", "1H", [c(HOUR_MS, 99, confirm=0)])   # 迟到的未收盘版本
        self.assertEqual(cache.snapshot("X", "1H")[0]["c"], 11)

    def test_revised_closed_bar_overwrites(self):
        cache = CandleCache(100)
        cache.merge("X", "1H", [c(HOUR_MS, 10)])
        cache.merge("X", "1H", [c(HOUR_MS, 10.5)])
        self.assertEqual(cache.snapshot("X", "1H")[0]["c"], 10.5)

    def test_capacity_keeps_newest(self):
        cache = CandleCache(5)
        cache.merge("X", "1H", [c(i * HOUR_MS, i) for i in range(12)])
        snap = cache.snapshot("X", "1H")
        self.assertEqual(len(snap), 5)
        self.assertEqual(snap[-1]["ts"], 11 * HOUR_MS)

    def test_snapshot_is_isolated_copy(self):
        cache = CandleCache(10)
        cache.merge("X", "1H", [c(HOUR_MS, 1)])
        snap = cache.snapshot("X", "1H")
        snap[0]["c"] = 999
        self.assertEqual(cache.snapshot("X", "1H")[0]["c"], 1)


class TimeframeTest(unittest.TestCase):
    def test_daily_and_weekly_utc8_open(self):
        # 2026-10-25(周日)16:00 UTC = 周一 00:00 UTC+8
        week_open = 1792944000000
        self.assertEqual(period_open("1W", week_open + 3 * 86400_000), week_open)
        self.assertEqual(period_open("1D", week_open + 5 * HOUR_MS), week_open)
        self.assertEqual(period_open("1D", week_open - 1), week_open - 86400_000)
        self.assertEqual(period_open("1D", week_open + 9 * HOUR_MS, daily_offset_ms=0) % 86400_000, 0)

    def test_weekly_not_stale_mid_week(self):
        cs = series("1W", NOW, 60)
        q = check_quality("1W", cs, NOW + 2 * 86400_000, 20, 8 * HOUR_MS)
        self.assertEqual(q["status"], "ok")
        q2 = check_quality("1W", cs, NOW + 8 * 86400_000, 20, 8 * HOUR_MS)
        self.assertEqual(q2["status"], "stale")

    def test_4h_and_1d_boundaries(self):
        for lv in ("4H", "1D"):
            cs = series(lv, NOW, 120)
            self.assertEqual(check_quality(lv, cs, NOW + HOUR_MS // 2, 60, 8 * HOUR_MS)["status"], "ok", lv)
            self.assertEqual(check_quality(lv, cs, NOW + DURATION_MS[lv] + 1, 60, 8 * HOUR_MS)["status"],
                             "stale", lv)

    def test_insufficient_history_is_reported_not_padded(self):
        cs = series("1W", NOW, 8)
        q = check_quality("1W", cs, NOW, 20, 8 * HOUR_MS, history_exhausted=True)
        self.assertEqual(q["status"], "insufficient")
        self.assertEqual(q["confirmed_count"], 8)


class ServiceTest(unittest.TestCase):
    def test_warmup_pages_history_then_incremental_with_overlap(self):
        data = market_data(NOW, 1500)
        okx = FakeOkx(data)
        svc = MarketService(settings(), client=okx)
        svc.update("BTC-USDT-SWAP", "1H", NOW)
        self.assertEqual(len(svc.cache.snapshot("BTC-USDT-SWAP", "1H")), 1200)
        self.assertTrue(any(k[0] == "history" for k in okx.calls))
        okx.calls.clear()
        svc.update("BTC-USDT-SWAP", "1H", NOW + 60_000)
        self.assertEqual(okx.calls, [("recent", "1H", 4)])   # 只取缺口 + 重叠

    def test_long_offline_backfills_gap(self):
        later = NOW + 500 * HOUR_MS
        full = series("1H", later, 1700)
        okx = FakeOkx({"1H": [x for x in full if x["ts"] <= expected_last_closed_open("1H", NOW, 0)]})
        svc = MarketService(settings(), client=okx)
        svc.update("X", "1H", NOW)
        okx.data["1H"] = full
        svc.update("X", "1H", later)
        snap = svc.cache.snapshot("X", "1H")
        conf = [x for x in snap if x["confirm"] == 1]
        gaps = sum(1 for a, b in zip(conf, conf[1:]) if b["ts"] - a["ts"] != HOUR_MS)
        self.assertEqual(gaps, 0)
        self.assertEqual(conf[-1]["ts"], expected_last_closed_open("1H", later, 0))
        self.assertLessEqual(len(snap), 1200)

    def test_batch_has_all_four_levels_and_quality(self):
        svc = MarketService(settings(), client=FakeOkx(market_data()))
        b = svc.batch("BTC-USDT-SWAP", NOW)
        self.assertEqual(set(b.levels), {"1H", "4H", "1D", "1W"})
        for lv in b.levels.values():
            self.assertEqual(lv.quality["status"], "ok", lv.level)
            self.assertIsNotNone(lv.forming)
        self.assertNotIn("candles", str(b.meta()))

    def test_fetch_error_keeps_old_cache_and_marks_stale(self):
        class Boom(FakeOkx):
            def recent(self, *a, **k):
                from market.okx import OkxError
                raise OkxError("down")
        svc = MarketService(settings(), client=FakeOkx(market_data()))
        svc.batch("X", NOW)
        svc.client = Boom({})
        b = svc.batch("X", NOW + 60_000)
        self.assertEqual(b.levels["1H"].quality["status"], "stale")


if __name__ == "__main__":
    unittest.main()
