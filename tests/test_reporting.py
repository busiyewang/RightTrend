"""详细报告的证据边界：不会把观察路径变成确认，也不会升级最终结论。"""

import copy
import json
import unittest

from analysis.combine import combine
from analysis.evidence import build_evidence
from analysis.reporting import build_detailed_report
from analysis.validate import review_technical
from market.service import MarketService
from tests.helpers import FakeOkx, NOW, market_data, settings


class DetailedReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = settings()
        batch = MarketService(cls.s, client=FakeOkx(market_data())).batch("ETH-USDT-SWAP", NOW)
        cls.base = build_evidence(batch)

    def report(self, ev=None, tech=None, community=None, model=None):
        ev = ev if ev is not None else copy.deepcopy(self.base)
        tech = tech or review_technical("not_configured", None, [], ev, self.s)
        comm = community or {"available": False, "note": "暂无社区日报"}
        return build_detailed_report("ETH-USDT-SWAP", ev, tech, combine(tech, comm), comm, model, self.s)

    def test_no_model_has_local_facts_but_no_participation_conclusion(self):
        rep = self.report()
        self.assertIn("无有效模型技术结论", rep["summary"])
        self.assertEqual(rep["plan"]["status"], "等待条件")
        self.assertTrue(all(lv["model_comment"] is None for lv in rep["levels"]))
        self.assertEqual([lv["level"] for lv in rep["levels"]], ["1W", "1D", "4H", "1H"])
        self.assertTrue(all(lv["close"] is not None for lv in rep["levels"]))
        self.assertNotIn('"candles"', json.dumps(rep))
        self.assertTrue(all(f["status"] == "未接入" for f in rep["funds"][:3]))

    def test_scenario_prices_all_reference_snapshot_levels_and_remain_conditional(self):
        rep = self.report()
        index = self.base["key_levels"]
        self.assertEqual([s["id"] for s in rep["scenarios"]], ["up", "down", "range"])
        for scenario in rep["scenarios"]:
            self.assertEqual(scenario["status"], "conditional")
            self.assertTrue(scenario["level_ids"])
            for lid in scenario["level_ids"]:
                self.assertIn(lid, index)
                self.assertIn(f"{index[lid]['price']:.8g}", scenario["setup"])
        self.assertIn("B2/B3", " ".join(rep["scenarios"][0]["confirmation"]))
        self.assertIn("S2/S3", " ".join(rep["scenarios"][1]["confirmation"]))

    def test_stale_required_data_or_focus_gap_blocks_scenarios(self):
        for gap in (False, True):
            ev = copy.deepcopy(self.base)
            if gap:
                ev["levels"]["4H"]["quality"]["gaps"] = 1
            else:
                ev["levels"]["1H"]["quality"].update(status="stale", issues=["缺少最近收盘"])
            rep = self.report(ev)
            self.assertTrue(all(s["status"] == "unavailable" for s in rep["scenarios"]))
            self.assertTrue(all(not s["level_ids"] for s in rep["scenarios"]))

    def test_missing_boundaries_does_not_invent_prices(self):
        ev = copy.deepcopy(self.base)
        ev["levels"]["4H"]["key_levels"] = []
        rep = self.report(ev)
        self.assertTrue(all(s["status"] == "unavailable" for s in rep["scenarios"]))

    def test_final_community_downgrade_still_controls_plan(self):
        tech = review_technical("not_configured", None, [], self.base, self.s)
        tech.update(verdict="long", model_status="ok", structure_confirmed=True, price_ok=True)
        tech["rr"] = {"status": "passed", "entry": 100.0, "stop": 98.0, "target1": 108.0,
                      "live": 100.0, "at_live": {"net_rr": 3.5}}
        comm = {"available": True, "sentiment": "bearish"}
        rep = self.report(tech=tech, community=comm)
        self.assertIn("观望", rep["headline"])
        self.assertEqual(rep["plan"]["status"], "等待条件")
        self.assertTrue(any("最终仍需等待" in item for item in rep["plan"]["items"]))

    def test_unconfirmed_or_future_signal_not_presented_as_locked(self):
        ev = copy.deepcopy(self.base)
        lv = ev["levels"]["4H"]
        lv["evidence"] = [{"id": "future-signal", "kind": "bsp", "level": "4H", "type": "B3",
                           "locked": True, "active": True, "known_at": NOW, "locked_at": NOW + 1,
                           "invalidated": None}]
        rep = self.report(ev)
        focus = next(row for row in rep["levels"] if row["level"] == "4H")
        metric = next(row for row in focus["metrics"] if row["name"] == "本地右侧证据")
        self.assertNotIn("future-signal", metric["value"])

    def test_downgraded_model_claim_never_appears_in_report_body(self):
        from tests.test_validate import B3, model, evidence
        # 使用真实的校验路径：编号存在，但信号尚未锁定。
        ev = copy.deepcopy(self.base)
        source = evidence(locked=False, locked_at=None)
        ev["index"][B3] = source["index"][B3]
        m = model(levels={lv: "右侧已经确认，可立即入场" for lv in ev["levels"]})
        tech = review_technical("ok", m, [], ev, self.s)
        rep = self.report(ev, tech, model=m)
        self.assertEqual(tech["verdict"], "watch")
        self.assertTrue(all(row["model_comment"] is None for row in rep["levels"]))
        self.assertNotIn("可立即入场", json.dumps(rep, ensure_ascii=False))

    def test_inactive_historical_signal_prices_not_shown_as_current_levels(self):
        ev = copy.deepcopy(self.base)
        ev["levels"]["4H"]["key_levels"].append(
            {"id": "L-4H-BSP-old-B3", "name": "过期的 B3", "price": 123.0})
        rep = self.report(ev)
        focus = next(row for row in rep["levels"] if row["level"] == "4H")
        self.assertNotIn("L-4H-BSP-old-B3", [p["id"] for p in focus["key_levels"]])


if __name__ == "__main__":
    unittest.main()
