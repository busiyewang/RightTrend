import unittest

import requests

from analysis.combine import combine, community_view
from analysis.schema import validate_tech
from analysis.validate import review_technical
from llm.deepseek import DeepSeekClient, LLMError
from tests.helpers import FakeResp, FakeSession, chat_body, settings

AS_OF = 1_793_098_890_000
H = 3_600_000
B3 = "E-1H-BSP-1793080000000-B3"


def evidence(**sig_over):
    sig = {"id": B3, "kind": "bsp", "level": "1H", "type": "B3", "price": 100.0,
           "anchor_ts": AS_OF - 10 * H, "known_at": AS_OF - 8 * H, "locked": True,
           "locked_at": AS_OF - 3 * H, "active": True, "invalidated": None, "score": 70}
    sig.update(sig_over)
    q = {"status": "ok", "issues": []}
    levels = {lv: {"quality": dict(q), "direction": {"dir": "long"}, "evidence": [], "key_levels": []}
              for lv in ("1H", "4H", "1D", "1W")}
    kl = {"L-1H-BSP-1793080000000-B3": {"id": "L-1H-BSP-1793080000000-B3", "price": 100.0},
          "L-4H-RECENT_HIGH": {"id": "L-4H-RECENT_HIGH", "price": 112.0},
          "L-1H-ZG": {"id": "L-1H-ZG", "price": 101.0},
          "LIVE": {"id": "LIVE", "price": 102.0}}
    return {"levels": levels, "index": {B3: sig}, "key_levels": kl, "live_price": 102.0,
            "analysis_as_of": AS_OF}


def model(**over):
    m = {"verdict": "long", "right_side": "confirmed", "signal_level": "1H", "signal_type": "B3",
         "evidence_ids": [B3], "levels": {"1W": "a", "1D": "b", "4H": "c", "1H": "d"},
         "entry": {"price": 102.0, "basis": "LIVE"},
         "stop": {"price": 99.8, "basis": "L-1H-BSP-1793080000000-B3", "offset_pct": -0.2},
         "targets": [{"price": 112.0, "basis": "L-4H-RECENT_HIGH"}],
         "confidence": 60, "reasons": ["1H B3 已锁定"], "risks": [], "invalidation": "x", "watch_next": "y"}
    m.update(over)
    return m


def review(m, ev=None, status="ok"):
    norm, errs = validate_tech(m) if m is not None else (None, [])
    return review_technical(status, norm, errs, ev or evidence(), settings())


class ReviewTest(unittest.TestCase):
    def test_valid_confirmed_long_passes(self):
        r = review(model())
        self.assertEqual(r["verdict"], "long", r)
        self.assertTrue(r["structure_confirmed"])
        self.assertEqual(r["rr"]["status"], "passed")
        self.assertGreaterEqual(r["rr"]["at_live"]["net_rr"], 2.0)

    def test_unlocked_signal_cannot_confirm(self):
        r = review(model(), evidence(locked=False, locked_at=None))
        self.assertEqual(r["verdict"], "watch")
        self.assertEqual(r["right_side"], "pending")
        self.assertIn("未锁定", " ".join(r["downgrades"]))

    def test_future_lock_is_rejected(self):
        r = review(model(), evidence(locked_at=AS_OF + H))
        self.assertEqual(r["verdict"], "watch")
        self.assertIn("未来信息", " ".join(r["downgrades"]))

    def test_invalidated_and_inactive_signals_rejected(self):
        self.assertEqual(review(model(), evidence(invalidated="跌破"))["verdict"], "watch")
        self.assertEqual(review(model(), evidence(active=False))["verdict"], "watch")

    def test_b1_is_not_right_side(self):
        r = review(model(signal_type="B1"), evidence(type="B1"))
        self.assertEqual(r["verdict"], "watch")
        self.assertFalse(r["structure_confirmed"])

    def test_fabricated_evidence_id(self):
        r = review(model(evidence_ids=["E-1H-BSP-1-B2"]))
        self.assertEqual(r["verdict"], "watch")
        self.assertIn("编造", " ".join(r["downgrades"]))

    def test_wrong_price_relation(self):
        m = model(stop={"price": 112.0, "basis": "L-4H-RECENT_HIGH"},
                  targets=[{"price": 99.8, "basis": "L-1H-BSP-1793080000000-B3", "offset_pct": -0.2}])
        r = review(m)
        self.assertEqual(r["verdict"], "watch")
        self.assertIn("价格关系错误", " ".join(r["downgrades"]))

    def test_price_not_matching_reference(self):
        r = review(model(stop={"price": 95.0, "basis": "L-1H-ZG"}))
        self.assertEqual(r["verdict"], "watch")
        self.assertEqual(r["rr"]["status"], "not_checked")

    def test_missing_prices_never_report_rr_passed(self):
        r = review(model(entry=None, stop=None, targets=[]))
        self.assertEqual(r["verdict"], "watch")
        self.assertNotEqual(r["rr"]["status"], "passed")

    def test_insufficient_rr_is_watch_but_structure_confirmed(self):
        r = review(model(targets=[{"price": 101.0, "basis": "L-1H-ZG"}]))
        self.assertTrue(r["structure_confirmed"])
        self.assertEqual(r["verdict"], "watch")

    def test_higher_level_conflict(self):
        ev = evidence()
        ev["levels"]["1D"]["direction"] = {"dir": "short"}
        r = review(model(), ev)
        self.assertEqual(r["verdict"], "watch")
        self.assertTrue(r["conflicts"])

    def test_stale_data_gives_insufficient(self):
        ev = evidence()
        ev["levels"]["4H"]["quality"] = {"status": "stale", "issues": ["落后 2 根"]}
        self.assertEqual(review(model(), ev)["verdict"], "insufficient_data")
        ev2 = evidence()
        ev2["levels"]["1W"]["quality"] = {"status": "insufficient", "issues": ["新币"]}
        self.assertEqual(review(model(), ev2)["verdict"], "long")   # 1W 只作背景,不阻断

    def test_schema_failures_are_unavailable(self):
        for bad in (model(verdict="buy"), model(right_side="yes"), {"verdict": "long"},
                    model(entry=102.0), model(verdict="long", right_side="pending"),
                    model(signal_type="S3")):
            r = review(bad)
            self.assertEqual(r["verdict"], "unavailable", bad)

    def test_model_failure_is_unavailable(self):
        self.assertEqual(review(None, status="timeout")["verdict"], "unavailable")
        self.assertEqual(review(None, status="not_configured")["verdict"], "unavailable")

    def test_model_watch_never_upgraded(self):
        self.assertEqual(review(model(verdict="watch"))["verdict"], "watch")


class CombineTest(unittest.TestCase):
    T_LONG = {"verdict": "long"}

    def comm(self, s):
        return {"available": True, "sentiment": s}

    def test_table(self):
        self.assertEqual(combine(self.T_LONG, self.comm("bullish"))["final"], "long")
        self.assertEqual(combine(self.T_LONG, self.comm("bearish"))["final"], "watch")
        self.assertEqual(combine({"verdict": "short"}, self.comm("bullish"))["final"], "watch")
        self.assertEqual(combine({"verdict": "watch"}, self.comm("bullish"))["final"], "watch")
        self.assertEqual(combine({"verdict": "watch"}, self.comm("bearish"))["final"], "watch")
        self.assertEqual(combine({"verdict": "unavailable"}, self.comm("bullish"))["final"], "no_signal")
        self.assertEqual(combine({"verdict": "insufficient_data"}, self.comm("bearish"))["final"], "no_signal")
        r = combine(self.T_LONG, {"available": False, "note": "社区日报已过期"})
        self.assertEqual(r["final"], "long")
        self.assertIn("社区参考不可用", r["notes"][0])

    def test_community_view_states(self):
        rep = {"report_id": "C1", "generated_at": AS_OF - H, "expires_at": AS_OF + H,
               "collection_status": "ok",
               "assets": [{"symbol": "BTC", "data_quality": "sufficient", "sentiment": "neutral"},
                          {"symbol": "ETH", "data_quality": "insufficient", "sentiment": "unknown"}]}
        self.assertEqual(community_view(rep, "BTC", AS_OF)["sentiment"], "neutral")
        self.assertEqual(community_view(rep, "ETH", AS_OF)["status"], "insufficient")
        self.assertEqual(community_view(rep, "BTC", AS_OF + 2 * H)["status"], "expired")
        failed = dict(rep, collection_status="blocked")
        self.assertEqual(community_view(failed, "BTC", AS_OF)["status"], "blocked")
        self.assertEqual(community_view(None, "BTC", AS_OF)["status"], "missing")


class DeepSeekTest(unittest.TestCase):
    def client(self, responses, retries=2):
        s = settings(deepseek_api_key="sk-test", deepseek_model="m", deepseek_max_retries=retries)
        sleeps = []
        c = DeepSeekClient(s, session=FakeSession(responses), sleep=sleeps.append)
        return c, sleeps

    def test_json_mode_and_success(self):
        c, _ = self.client([FakeResp(200, chat_body('{"verdict":"watch"}'))])
        r = c.chat_json("sys", "user")
        self.assertEqual(r.data, {"verdict": "watch"})
        self.assertEqual(c.sess.posts[0]["response_format"], {"type": "json_object"})

    def test_failures(self):
        cases = [(chat_body(""), "empty"), (chat_body('{"a":', finish="length"), "truncated"),
                 (chat_body("not json"), "invalid_json"), (chat_body("[1,2]"), "invalid_json"),
                 ({"choices": []}, "bad_response")]
        for body, code in cases:
            c, _ = self.client([FakeResp(200, body)])
            with self.assertRaises(LLMError) as cm:
                c.chat_json("s", "u")
            self.assertEqual(cm.exception.code, code)

    def test_retry_after_then_success(self):
        c, sleeps = self.client([FakeResp(429, {}, headers={"Retry-After": "7"}),
                                 FakeResp(200, chat_body('{"ok":1}'))])
        r = c.chat_json("s", "u")
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(r.attempts), 2)

    def test_rate_limit_exhausted(self):
        c, _ = self.client([FakeResp(429, {})] * 3)
        with self.assertRaises(LLMError) as cm:
            c.chat_json("s", "u")
        self.assertEqual(cm.exception.code, "rate_limited")
        self.assertEqual(len(cm.exception.attempts), 3)

    def test_timeout_is_uncertain(self):
        c, _ = self.client([requests.Timeout("read timeout")], retries=0)
        with self.assertRaises(LLMError) as cm:
            c.chat_json("s", "u")
        self.assertEqual(cm.exception.code, "timeout")
        self.assertEqual(cm.exception.attempts[0]["outcome"], "uncertain_timeout")

    def test_not_configured(self):
        c = DeepSeekClient(settings())
        with self.assertRaises(LLMError) as cm:
            c.chat_json("s", "u")
        self.assertEqual(cm.exception.code, "not_configured")


if __name__ == "__main__":
    unittest.main()
