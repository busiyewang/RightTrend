"""独立 worker:唯一承担调度的进程。文件锁保证单实例;Web 进程只读状态、提交手动任务。"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading
import time

from jobs.runner import build_context, run_daily_job, run_hourly_job
from jobs.scheduler import Scheduler

log = logging.getLogger("righttrend.worker")
TICK_MAX_SLEEP = 5.0


class AlreadyRunning(RuntimeError):
    pass


def acquire_lock(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise AlreadyRunning(f"已有 worker 在运行(锁文件 {path})")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def run_worker(settings, warmup: bool = True):
    lock = acquire_lock(settings.lock_path)
    ctx = build_context(settings)
    ctx.store.ensure_watchlist(settings.default_watchlist)
    ctx.store.recover_running()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker 启动 pid=%s 关注=%s 模型=%s 每日=%s %s", os.getpid(), ctx.store.watchlist(),
             settings.llm_status, settings.daily_time, settings.schedule_tz)
    if warmup:
        try:
            ctx.market.warmup(ctx.store.watchlist())
            log.info("K线预热完成: %s", [(x["inst_id"], x["level"], x["count"]) for x in ctx.market.cache.stats()])
        except Exception:
            log.exception("预热失败,将在任务中重试")
    sched = Scheduler(ctx, run_hourly_job, run_daily_job, clock=lambda: int(time.time() * 1000))
    try:
        while not stop.is_set():
            try:
                sched.tick()
            except Exception:     # 调度循环永不因单次异常退出
                log.exception("调度 tick 异常")
            now = int(time.time() * 1000)
            wait = min(TICK_MAX_SLEEP, max(0.5, (sched.next_wakeup(now) - now) / 1000))
            stop.wait(wait)
    finally:
        log.info("worker 停止")
        lock.close()
