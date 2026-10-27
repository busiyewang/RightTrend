import copy
import math
import unittest

from analysis.chanlun.indicators import EMA_PERIODS, compute_all
from analysis.evidence import build_evidence
from analysis.market_summary import summarize_levels
from analysis.snapshot import COLUMNS, build_model_input
from market.service import MarketService
from tests.helpers import NOW, FakeOkx, market_data, series, settings


def candles_from_closes(closes):
    return [{"ts": i * 3600_000, "o": value, "h": value + 1, "l": value - 1,
             "c": value, "vol": 100, "confirm": 1} for i, value in enumerate(closes)]


def evidence_for(data):
    batch = MarketService(settings(), client=FakeOkx(data)).batch("BTC-USDT-SWAP", NOW)
    return batch, build_evidence(batch)


class EmaMathTest(unittest.TestCase):
    def test_impulse_has_closed_form_decay_and_no_premature_values(self):
        # 首点 200,后续全部 100:EMA(t)=100+100*(1-alpha)^t。
        result = compute_all(candles_from_closes([200] + [100] * 100))
        for period in EMA_PERIODS:
            values = result["ema"][f"ema{period}"]
            self.assertEqual(values[:period - 1], [None] * (period - 1))
            alpha = 2 / (period + 1)
            for index in (period - 1, 100):
                expected = 100 + 100 * math.pow(1 - alpha, index)
                self.assertAlmostEqual(values[index], expected, places=5)

    def test_future_prices_cannot_change_existing_ema_values(self):
        history = [100 + i / 10 for i in range(90)]
        earlier = compute_all(candles_from_closes(history))
        later = compute_all(candles_from_closes(history + [100000, 1, 50000]))
        for name in earlier["ema"]:
            self.assertEqual(earlier["ema"][name], later["ema"][name][:90])


class MarketSummaryTest(unittest.TestCase):
    def test_forming_extremes_cannot_change_closed_report_or_ema_references(self):
        data = market_data(n=160)
        altered = copy.deepcopy(data)
        for candles in altered.values():
            candles[-1].update(c=1e9, h=1e9, l=0.001, vol=1e15)
        batch, normal = evidence_for(data)
        _, extreme = evidence_for(altered)
        self.assertNotEqual(normal["live_price"], extreme["live_price"])
        self.assertEqual(summarize_levels(normal), summarize_levels(extreme))
        for level in batch.levels:
            self.assertEqual(normal["key_levels"][f"L-{level}-EMA20"],
                             extreme["key_levels"][f"L-{level}-EMA20"])
        summary = summarize_levels(normal)
        self.assertEqual([s["level"] for s in summary], ["1W", "1D", "4H", "1H"])
        for block in summary:
            lv = batch.levels[block["level"]]
            self.assertEqual(block["close"], lv.confirmed[-1]["c"])
            self.assertEqual(block["last_closed_at"], lv.quality["last_closed_at"])

    def test_short_history_does_not_invent_long_ema_or_volume_comparison(self):
        data = market_data(n=160)
        data["1W"] = series("1W", NOW, 12)
        _, evidence = evidence_for(data)
        weekly = summarize_levels(evidence)[0]
        self.assertEqual(weekly["quality"], "insufficient")
        self.assertIn("L-1W-EMA10", evidence["key_levels"])
        self.assertNotIn("L-1W-EMA20", evidence["key_levels"])
        self.assertNotIn("L-1W-EMA80", evidence["key_levels"])
        metrics = {m["name"]: m for m in weekly["metrics"]}
        self.assertIn("12 根", metrics["数据状态"]["value"])
        self.assertIn("未就绪", metrics["EMA 指数均线"]["value"])
        self.assertIn("80 根", metrics["EMA 排列"]["interpretation"])
        self.assertIn("不计算量比", metrics["成交量"]["interpretation"])

    def test_ema_latest_survives_prompt_budget_trimming_without_wider_rows(self):
        batch, evidence = evidence_for(market_data(n=160))
        cfg = settings()
        data, _, meta = build_model_input("BTC-USDT-SWAP", evidence, batch, cfg.model_bars, 1)
        self.assertTrue(meta["trim"])
        self.assertEqual(set(data["levels"]), {"1H", "4H", "1D", "1W"})
        self.assertEqual(len(COLUMNS), 14)
        for level in data["levels"]:
            block = data["levels"][level]
            self.assertNotIn("indicator_state", block)
            self.assertEqual(set(block["ema_latest"]), {f"ema{n}" for n in EMA_PERIODS})
            for period in EMA_PERIODS:
                self.assertEqual(block["ema_latest"][f"ema{period}"],
                                 evidence["key_levels"][f"L-{level}-EMA{period}"]["price"])
            self.assertTrue(all(len(row) == len(COLUMNS) for row in block["bars"]))

    def test_negative_macd_shortening_is_not_described_as_positive(self):
        # 平台后持续下跌,末根停跌;两线仍在零轴下,负柱开始缩短。
        candles = candles_from_closes([200] * 60 + [200 - i for i in range(15)] + [186])
        indicators = compute_all(candles)
        macd = indicators["macd"]
        self.assertLess(macd["dif"][-1], 0)
        self.assertLess(macd["dea"][-1], 0)
        self.assertLess(indicators["macd"]["hist"][-1], 0)
        self.assertGreater(indicators["macd"]["hist"][-1], indicators["macd"]["hist"][-2])
        evidence = {"levels": {"1H": {"payload": {"candles": candles, "indicators": indicators}}}}
        hourly = summarize_levels(evidence)[-1]
        metric = next(m for m in hourly["metrics"] if m["name"] == "MACD(10,20,5)")
        self.assertIn("均在零轴下方", metric["interpretation"])
        self.assertIn("负柱缩短", metric["interpretation"])
        self.assertIn("不能单独确认", metric["interpretation"])

    def test_missing_levels_stay_visible_without_fabricated_prices(self):
        summaries = summarize_levels({})
        self.assertEqual(len(summaries), 4)
        for summary in summaries:
            self.assertEqual(summary["quality"], "empty")
            self.assertIsNone(summary["close"])
            self.assertIsNone(summary["last_closed_at"])
            self.assertEqual(summary["key_levels"], [])


if __name__ == "__main__":
    unittest.main()
