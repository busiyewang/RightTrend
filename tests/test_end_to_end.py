"""端到端(离线):假 OKX + 假 DeepSeek,检查报告结构、降级行为和"不落盘K线/密钥"。"""

import json
import logging
import unittest

from jobs.runner import Context, analyze, community_daily, run_hourly_job
from llm.deepseek import DeepSeekClient
from market.service import MarketService
from storage.db import Store
from tests.helpers import NOW, FakeOkx, FakeResp, FakeSession, chat_body, market_data, settings

KEY = "sk-SECRET-should-never-be-stored"


def ctx_with(llm_responses=None, **kw):
    s = settings(deepseek_api_key=KEY if llm_responses is not None else "",
                 deepseek_model="test-model" if llm_responses is not None else "",
                 deepseek_max_retries=0, **kw)
    store = Store(":memory:")
    store.ensure_watchlist(["BTC-USDT-SWAP"])
    market = MarketService(s, client=FakeOkx(market_data()))
    llm = DeepSeekClient(s, session=FakeSession(llm_responses or []), sleep=lambda x: None)
    return Context(s, store, market, llm)


def dump_db(store):
    c = store.conn()
    out = []
    for t in ("reports", "jobs", "community_reports", "community_posts"):
        out.extend(json.dumps([dict(r) for r in c.execute(f"SELECT * FROM {t}").fetchall()], ensure_ascii=False))
    return "".join(out)


class EndToEndTest(unittest.TestCase):
    def test_no_key_saves_local_report_as_not_configured(self):
        ctx = ctx_with(None)
        jid = ctx.store.claim_job("hourly", "BTC-USDT-SWAP", "k", NOW)
        run_hourly_job(ctx, jid, "BTC-USDT-SWAP")
        job = ctx.store.job(jid)
        self.assertEqual((job["status"], job["error_code"]), ("skipped", "llm_not_configured"))
        rep = ctx.store.reports()[0]
        self.assertEqual(rep["model_status"], "not_configured")
        self.assertEqual(rep["final"]["final"], "no_signal")
        self.assertEqual(set(rep["input_meta"]["bars_sent"]), {"1H", "4H", "1D", "1W"})

    def test_fabricated_model_output_is_watch_and_nothing_sensitive_stored(self):
        bogus = {"verdict": "long", "right_side": "confirmed", "signal_level": "1H", "signal_type": "B2",
                 "evidence_ids": ["E-1H-BSP-123-B2"], "levels": {"1W": "a", "1D": "b", "4H": "c", "1H": "d"},
                 "entry": None, "stop": None, "targets": [], "confidence": 90,
                 "reasons": ["编造"], "risks": [], "invalidation": "", "watch_next": ""}
        ctx = ctx_with([FakeResp(200, chat_body(json.dumps(bogus)))])
        with self.assertLogs("righttrend", level="INFO") as logs:
            jid = ctx.store.claim_job("hourly", "BTC-USDT-SWAP", "k", NOW)
            run_hourly_job(ctx, jid, "BTC-USDT-SWAP")
            logging.getLogger("righttrend.test").info("done")
        rep = ctx.store.reports()[0]
        self.assertEqual(rep["technical"]["verdict"], "watch")
        self.assertEqual(rep["final"]["final"], "watch")
        sent = ctx.llm.sess.posts[0]["messages"][1]["content"]
        self.assertIn('"bars"', sent)                     # 模型确实收到了K线
        db = dump_db(ctx.store)
        self.assertNotIn(KEY, db)
        self.assertNotIn('"bars"', db)                    # 数据库不含K线表
        self.assertNotIn('"candles"', db)
        self.assertNotIn("Cookie", db)
        for line in logs.output:
            self.assertNotIn('"bars"', line)
            self.assertNotIn(KEY, line)
        self.assertLess(len(db), 60_000)

    def test_truncated_model_output_is_failed(self):
        ctx = ctx_with([FakeResp(200, chat_body('{"verdict":"lo', finish="length"))])
        jid = ctx.store.claim_job("hourly", "BTC-USDT-SWAP", "k", NOW)
        run_hourly_job(ctx, jid, "BTC-USDT-SWAP")
        self.assertEqual(ctx.store.job(jid)["status"], "failed")
        self.assertEqual(ctx.store.reports()[0]["final"]["final"], "no_signal")

    def test_hourly_report_references_valid_community_report_only(self):
        ctx = ctx_with(None)
        from community.sources import ImportSource
        posts = [{"post_id": f"p{i}", "url": f"https://www.okx.com/orbit/post/p{i}", "author_id": f"a{i % 6}",
                  "publish_time": NOW - i * 60_000, "text": f"BTC 观点 {i} 论据 {i * 31}"} for i in range(12)]
        crep = community_daily(ctx, now=NOW - 60_000, source=ImportSource(posts))
        self.assertEqual(crep["model_status"], "not_configured")
        rep = analyze(ctx, "BTC-USDT-SWAP", now=NOW)
        self.assertEqual(rep["community_report_id"], crep["report_id"])
        self.assertEqual(rep["community"]["status"], "unknown")   # 无模型 → 情绪未知 → 不参与
        late = analyze(ctx, "BTC-USDT-SWAP", now=NOW + 27 * 3_600_000)
        self.assertIsNone(late["community_report_id"])            # 过期日报不再参与
        self.assertEqual(ctx.store.report(rep["report_id"])["community_report_id"], crep["report_id"])


if __name__ == "__main__":
    unittest.main()
