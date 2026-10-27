import unittest
from collections import Counter
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from jobs.scheduler import Scheduler, current_daily_slot, daily_run_at
from storage.db import Store
from tests.helpers import settings

H = 3_600_000
LA = ZoneInfo("America/Los_Angeles")


def utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Ctx:
    def __init__(self, store, s):
        self.store, self.settings = store, s


class Harness:
    def __init__(self, start, store=None, task_ms=0):
        self.s = settings()
        self.store = store or Store(":memory:")
        self.store.ensure_watchlist(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        self.clock = Clock(start)
        self.runs = []
        self.task_ms = task_ms
        self.sched = Scheduler(Ctx(self.store, self.s), self.hourly, self.daily, self.clock)

    def hourly(self, ctx, jid, inst, at):
        self.runs.append(("hourly", inst, self.clock.t))
        self.clock.t += self.task_ms
        self.store.finish_job(jid, "succeeded")

    def daily(self, ctx, jid, date):
        self.runs.append(("daily", date, self.clock.t))
        self.store.finish_job(jid, "succeeded")

    def advance(self, until, step=30_000):
        while self.clock.t < until:
            self.sched.tick()
            self.clock.t += step


class SchedulerTest(unittest.TestCase):
    def test_24_hours_each_inst_24_distinct_runs_and_one_daily(self):
        start = utc(2026, 10, 27, 0, 0, 0)
        h = Harness(start)
        h.advance(start + 24 * H)
        hourly = Counter(inst for kind, inst, _ in h.runs if kind == "hourly")
        self.assertEqual(hourly, {"BTC-USDT-SWAP": 24, "ETH-USDT-SWAP": 24})
        for kind, _, t in h.runs:
            if kind == "hourly":
                self.assertGreaterEqual(t % H, 90_000)
        daily = [r for r in h.runs if r[0] == "daily"]
        # 00:00 UTC 启动时 LA 是前一天 17:00 → 先补跑前一天的日报(仍在有效期内),当天 09:10 再跑一次
        self.assertEqual([d[1] for d in daily], ["2026-10-26", "2026-10-27"])
        self.assertEqual(daily[1][2] >= daily_run_at(datetime(2026, 10, 27).date(), "09:10", LA), True)

    def test_restart_does_not_duplicate(self):
        start = utc(2026, 10, 27, 10, 0, 0)
        h = Harness(start)
        h.advance(start + 2 * H)
        n = len(h.runs)
        h2 = Harness(start + 2 * H - 10_000, store=h.store)   # 新进程,同一数据库
        h2.advance(start + 2 * H + 60_000)
        self.assertEqual(len(h2.runs), 0)
        self.assertEqual(n, 2 * 2 + 1)

    def test_start_just_after_top_of_hour_waits_for_delay(self):
        start = utc(2026, 10, 27, 10, 0, 30)
        h = Harness(start)
        h.sched.daily_enabled = False
        h.sched.tick()
        self.assertEqual(h.runs, [])
        h.clock.t = utc(2026, 10, 27, 10, 1, 30)
        h.sched.tick()
        self.assertEqual(len(h.runs), 2)
        self.assertEqual(h.store.jobs()[0]["slot_key"], "2026-10-27T10:00Z")

    def test_downtime_marks_missed_without_burst(self):
        start = utc(2026, 10, 27, 0, 0, 0)
        h = Harness(start)
        h.sched.daily_enabled = False
        h.advance(start + 3 * H)                       # 00/01/02 点各执行
        h.clock.t = start + 10 * H + 20 * 60_000       # 停机到 10:20 再启动
        before = len(h.runs)
        h.sched.tick()
        self.assertEqual(len(h.runs) - before, 2)      # 只补当前 10 点这一时段
        missed = [j for j in h.store.jobs(200) if j["error_code"] == "missed"]
        self.assertEqual(len(missed), 2 * 7)           # 03..09 点每币种记 missed

    def test_long_task_next_hour_is_recorded(self):
        start = utc(2026, 10, 27, 0, 0, 0)
        h = Harness(start, task_ms=70 * 60_000)        # 每个任务 70 分钟
        h.sched.daily_enabled = False
        h.advance(start + 4 * H)
        jobs = h.store.jobs(200)
        slots = Counter((j["scope"], j["slot_key"]) for j in jobs)
        self.assertTrue(all(v == 1 for v in slots.values()))
        hours = {j["slot_key"] for j in jobs}
        self.assertTrue({"2026-10-27T00:00Z", "2026-10-27T01:00Z", "2026-10-27T02:00Z"} <= hours)
        self.assertTrue(any(j["error_code"] == "missed" for j in jobs))

    def test_dst_transitions_daily_once_at_local_time(self):
        for day in ((2026, 11, 1), (2027, 3, 14)):        # 夏令时结束 / 开始
            start = utc(*day, 0, 0, 0) - 24 * H
            h = Harness(start)
            h.sched.hourly_enabled = False
            h.advance(start + 72 * H, step=60_000)
            daily = [r for r in h.runs if r[0] == "daily"]
            dates = [d[1] for d in daily]
            self.assertEqual(len(dates), len(set(dates)))
            for _, date, t in daily[1:]:
                local = datetime.fromtimestamp(t / 1000, LA)
                self.assertEqual((local.hour, local.minute), (9, 10), (date, local))

    def test_current_daily_slot(self):
        t = daily_run_at(datetime(2026, 11, 1).date(), "09:10", LA)
        self.assertEqual(current_daily_slot(t - 1, "09:10", LA)[0], "2026-10-31")
        self.assertEqual(current_daily_slot(t, "09:10", LA)[0], "2026-11-01")
        # 11 月 1 日 PST(UTC-8):09:10 本地 = 17:10 UTC
        self.assertEqual(datetime.fromtimestamp(t / 1000, timezone.utc).hour, 17)

    def test_manual_jobs_run_once(self):
        h = Harness(utc(2026, 10, 27, 10, 30))
        h.sched.daily_enabled = h.sched.hourly_enabled = False
        h.store.claim_job("manual_hourly", "BTC-USDT-SWAP", "1", None, status="pending")
        h.sched.tick()
        h.sched.tick()
        self.assertEqual([r[1] for r in h.runs], ["BTC-USDT-SWAP"])


if __name__ == "__main__":
    unittest.main()
