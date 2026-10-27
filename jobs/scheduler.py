"""调度:时钟可注入,便于用模拟时钟测试 24 小时、重启、整点延迟与夏令时。

- 小时任务:每个 UTC 整点后 hourly_delay_sec 秒;唯一键 (hourly, inst_id, 整点UTC)。
  某个整点只在它自己的小时内有效;过了这一小时仍未执行的记为 skipped/missed,不补发。
- 每日任务:调度时区 daily_time;唯一键 (daily, '*', 逻辑日期)。最近一个已到期的逻辑日
  在下一次到期前都可补跑;更早的记 missed(最多回溯 MISSED_LOOKBACK_DAYS 天)。
- SQLite 唯一约束保证重复 tick、重启、多次调用都不会重复执行。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:   # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

HOUR_MS = 3600_000
MISSED_LOOKBACK_HOURS = 48
MISSED_LOOKBACK_DAYS = 7
log = logging.getLogger("righttrend.scheduler")


def slot_key_hour(slot_ms: int) -> str:
    return datetime.fromtimestamp(slot_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def daily_run_at(date, hhmm: str, tz) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    local = datetime(date.year, date.month, date.day, h, m, tzinfo=tz)
    # 经 UTC 往返规范化:落在夏令时跳过区间的时刻会被推到有效时间
    utc = local.astimezone(timezone.utc)
    return int(utc.timestamp() * 1000)


def current_daily_slot(now_ms: int, hhmm: str, tz) -> Tuple[str, int]:
    """不晚于 now 的最近一个每日到期点 → (逻辑日期, 到期时间 ms)。"""
    d = datetime.fromtimestamp(now_ms / 1000, tz).date()
    run = daily_run_at(d, hhmm, tz)
    if now_ms < run:
        d = d - timedelta(days=1)
        run = daily_run_at(d, hhmm, tz)
    return d.isoformat(), run


class Scheduler:
    def __init__(self, ctx, run_hourly: Callable, run_daily: Callable,
                 clock: Callable[[], int], hourly_enabled=True, daily_enabled=True):
        self.ctx = ctx
        self.store = ctx.store
        self.s = ctx.settings
        self.run_hourly = run_hourly     # (ctx, job_id, inst_id, scheduled_at)
        self.run_daily = run_daily       # (ctx, job_id, logical_date)
        self.clock = clock
        self.tz = ZoneInfo(self.s.schedule_tz)
        self.hourly_enabled = hourly_enabled
        self.daily_enabled = daily_enabled

    # ----- 计算 -----
    def hourly_due(self, now: int) -> Optional[int]:
        slot = now // HOUR_MS * HOUR_MS
        return slot if now >= slot + self.s.hourly_delay_sec * 1000 else None

    def next_wakeup(self, now: int) -> int:
        slot = now // HOUR_MS * HOUR_MS
        nxt = slot + self.s.hourly_delay_sec * 1000
        if now >= nxt:
            nxt += HOUR_MS
        _, drun = current_daily_slot(now, self.s.daily_time, self.tz)
        d_next = daily_run_at((datetime.fromtimestamp(drun / 1000, self.tz) + timedelta(days=1)).date(),
                              self.s.daily_time, self.tz)
        return min(nxt, d_next)

    def _mark_missed_hours(self, inst: str, now: int):
        last = self.store.latest_slot("hourly", inst)
        cur = now // HOUR_MS * HOUR_MS
        if last is None:
            return
        last_ms = int(datetime.strptime(last, "%Y-%m-%dT%H:00Z").replace(tzinfo=timezone.utc).timestamp() * 1000)
        start = max(last_ms + HOUR_MS, cur - MISSED_LOOKBACK_HOURS * HOUR_MS)
        t = start
        while t < cur:
            key = slot_key_hour(t)
            jid = self.store.claim_job("hourly", inst, key, t + self.s.hourly_delay_sec * 1000,
                                       status="skipped")
            if jid:
                self.store.finish_job(jid, "skipped", error_code="missed",
                                      error="调度器未在该小时内运行,不补发")
            t += HOUR_MS

    def _mark_missed_days(self, logical_date: str):
        d = datetime.strptime(logical_date, "%Y-%m-%d").date()
        last = self.store.latest_slot("daily")
        if last is None:
            return
        ld = datetime.strptime(last, "%Y-%m-%d").date()
        x = max(ld + timedelta(days=1), d - timedelta(days=MISSED_LOOKBACK_DAYS))
        while x < d:
            jid = self.store.claim_job("daily", "*", x.isoformat(),
                                       daily_run_at(x, self.s.daily_time, self.tz), status="skipped")
            if jid:
                self.store.finish_job(jid, "skipped", error_code="missed", error="调度器未运行,不补发")
            x += timedelta(days=1)

    # ----- 执行 -----
    def tick(self) -> List[str]:
        """执行所有到期且未记录的任务,返回本次执行的 job_id。"""
        done: List[str] = []
        now = self.clock()
        if self.daily_enabled:
            date, run_at = current_daily_slot(now, self.s.daily_time, self.tz)
            self._mark_missed_days(date)
            jid = self.store.claim_job("daily", "*", date, run_at)
            if jid:
                self.run_daily(self.ctx, jid, date)
                done.append(jid)
        if self.hourly_enabled:
            for inst in self.store.watchlist():
                now = self.clock()
                self._mark_missed_hours(inst, now)
                slot = self.hourly_due(now)
                if slot is None:
                    continue
                jid = self.store.claim_job("hourly", inst, slot_key_hour(slot),
                                           slot + self.s.hourly_delay_sec * 1000)
                if jid:
                    self.run_hourly(self.ctx, jid, inst, slot + self.s.hourly_delay_sec * 1000)
                    done.append(jid)
        for job in self.store.pending_manual():
            if not self.store.start_pending(job["job_id"]):
                continue
            if job["job_type"] == "manual_hourly":
                self.run_hourly(self.ctx, job["job_id"], job["scope"], None)
            elif job["job_type"] == "manual_daily":
                self.run_daily(self.ctx, job["job_id"], None)
            done.append(job["job_id"])
        return done
