"""单次任务:小时技术分析(每个币种一次)与每日社区日报。调度器和命令行共用。"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from analysis.chanlun import CHAN_VERSION
from analysis.combine import combine, community_view
from analysis.evidence import build_evidence
from analysis.schema import validate_tech
from analysis.snapshot import build_model_input
from analysis.validate import STRATEGY_VERSION, review_technical
from app.config import Settings
from community.process import base_symbol
from community.report import build_report
from community.sources import CommunitySource, ImportSource, OrbitPublicSource
from llm.deepseek import DeepSeekClient, LLMError
from llm.prompts import TECH_PROMPT_VERSION, TECH_SYSTEM_PROMPT
from market.service import MarketService
from storage.db import Store

log = logging.getLogger("righttrend.jobs")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Context:
    settings: Settings
    store: Store
    market: MarketService
    llm: DeepSeekClient
    source_factory: Optional[Callable[[], CommunitySource]] = None

    def community_source(self) -> CommunitySource:
        if self.source_factory:
            return self.source_factory()
        s = self.settings
        return OrbitPublicSource(s.okx_base, s.community_locale, s.community_max_pages,
                                 s.community_max_details, s.community_request_interval)


def build_context(settings: Settings, store: Optional[Store] = None) -> Context:
    return Context(settings, store or Store(settings.db_path), MarketService(settings),
                   DeepSeekClient(settings))


# ---------------- 小时技术分析 ----------------
def prepare(ctx: Context, inst_id: str, now: Optional[int] = None, refresh: bool = True):
    s = ctx.settings
    batch = ctx.market.batch(inst_id, now, refresh=refresh)
    ev = build_evidence(batch, s.stop_buffer)
    data, text, meta = build_model_input(inst_id, ev, batch, s.model_bars, s.tech_input_max_chars)
    return batch, ev, data, text, meta


def analyze(ctx: Context, inst_id: str, job_id: Optional[str] = None,
            now: Optional[int] = None, scheduled_at: Optional[int] = None) -> Dict:
    s = ctx.settings
    batch, ev, data, text, meta = prepare(ctx, inst_id, now)
    as_of = batch.analysis_as_of
    model_status, model_out, schema_errs, raw, err, usage, attempts = "not_configured", None, [], None, None, {}, []
    model_name = s.deepseek_model or None
    if ctx.llm.configured:
        if meta["over_budget"]:
            model_status, err = "skipped_over_budget", f"输入 {meta['chars']} 字符超过预算"
        else:
            try:
                r = ctx.llm.chat_json(TECH_SYSTEM_PROMPT, "分析数据(JSON):\n" + text)
                raw, usage, attempts, model_name = r.data, r.usage, r.attempts, r.model
                model_out, schema_errs = validate_tech(r.data)
                model_status = "ok"
            except LLMError as e:
                model_status, err, attempts = e.code, e.msg, e.attempts
    tech = review_technical(model_status, model_out, schema_errs, ev, s, err)
    sym = base_symbol(inst_id)
    creport = ctx.store.valid_community_report(as_of)
    comm = community_view(creport, sym, as_of)
    final = combine(tech, comm)
    rep = {
        "report_id": "R" + uuid.uuid4().hex[:15], "job_id": job_id, "inst_id": inst_id,
        "analysis_as_of": as_of, "scheduled_at": scheduled_at, "created_at": now_ms(),
        "strategy_version": STRATEGY_VERSION, "chan_version": CHAN_VERSION,
        "model": model_name, "prompt_version": TECH_PROMPT_VERSION, "model_status": model_status,
        "model_error": err, "llm_calls": len(attempts), "tokens": int(usage.get("total_tokens") or 0),
        "usage": usage, "llm_attempts": attempts,
        "data_quality": tech["data_quality"], "input_meta": {**meta, "batch": batch.meta()},
        "live_price": batch.live_price,
        "model_raw": raw, "schema_errors": schema_errs,
        "technical": tech, "community": comm,
        "community_report_id": comm.get("report_id"), "final": final,
        "levels_local": {lv: {"direction": ev["levels"][lv].get("direction"),
                              "local_action": (ev["levels"][lv].get("local_decision") or {}).get("action")}
                         for lv in ev["levels"]},
    }
    ctx.store.save_report(rep)
    return rep


def run_hourly_job(ctx: Context, job_id: str, inst_id: str, scheduled_at: Optional[int] = None):
    try:
        rep = analyze(ctx, inst_id, job_id, scheduled_at=scheduled_at)
    except Exception as e:      # 单币种失败不能终止调度器
        log.exception("小时任务失败 %s", inst_id)
        ctx.store.finish_job(job_id, "failed", error_code="exception", error=str(e)[:500])
        return None
    ms = rep["model_status"]
    uncertain = any(a.get("outcome") == "uncertain_timeout" for a in rep["llm_attempts"])
    fields = dict(report_id=rep["report_id"], model=rep["model"], prompt_version=rep["prompt_version"],
                  llm_calls=rep["llm_calls"], tokens=rep["tokens"], uncertain=1 if uncertain else 0)
    if ms == "ok":
        ctx.store.finish_job(job_id, "succeeded", **fields)
    elif ms == "not_configured":
        ctx.store.finish_job(job_id, "skipped", error_code="llm_not_configured",
                             error="未配置 DeepSeek,仅保存本地证据报告", **fields)
    else:
        ctx.store.finish_job(job_id, "failed", error_code=ms, error=rep.get("model_error"), **fields)
    log.info("小时分析 %s → %s (%s)", inst_id, rep["final"]["label"], ms)
    return rep


# ---------------- 每日社区日报 ----------------
def community_daily(ctx: Context, job_id: Optional[str] = None, now: Optional[int] = None,
                    logical_date: Optional[str] = None, source: Optional[CommunitySource] = None) -> Dict:
    now = now or now_ms()
    symbols = sorted({base_symbol(i) for i in ctx.store.watchlist()}) or \
        sorted({base_symbol(i) for i in ctx.settings.default_watchlist})
    src = source or ctx.community_source()
    rep = build_report(src, ctx.llm, ctx.settings, symbols, now, logical_date)
    rep["job_id"] = job_id
    posts = rep.pop("_posts", [])
    for p in posts:
        p.setdefault("collected_at", now)
    ctx.store.save_community_report(rep)
    if posts:
        ctx.store.save_posts(posts, rep["report_id"])
    return rep


def run_daily_job(ctx: Context, job_id: str, logical_date: Optional[str],
                  import_path: Optional[str] = None):
    try:
        src = ImportSource.from_file(import_path) if import_path else None
        rep = community_daily(ctx, job_id, logical_date=logical_date, source=src)
    except Exception as e:
        log.exception("每日社区任务失败")
        ctx.store.finish_job(job_id, "failed", error_code="exception", error=str(e)[:500])
        return None
    uncertain = any(a.get("outcome") == "uncertain_timeout" for a in rep["llm_attempts"])
    fields = dict(report_id=rep["report_id"], model=rep.get("model"), prompt_version=rep["prompt_version"],
                  llm_calls=rep["llm_calls"], tokens=rep["tokens"], uncertain=1 if uncertain else 0)
    cs, ms = rep["collection_status"], rep["model_status"]
    if cs in ("failed", "login_required", "blocked"):
        ctx.store.finish_job(job_id, "failed", error_code=f"collection_{cs}",
                             error=rep.get("collection_error"), **fields)
    elif ms in ("failed",):
        ctx.store.finish_job(job_id, "failed", error_code="llm_failed", error=rep.get("model_error"), **fields)
    elif ms == "not_configured":
        ctx.store.finish_job(job_id, "skipped", error_code="llm_not_configured",
                             error="未配置 DeepSeek,仅保存采集统计", **fields)
    else:
        ctx.store.finish_job(job_id, "succeeded", error_code=None if cs == "ok" else f"collection_{cs}",
                             error=rep.get("collection_error") or rep.get("model_error"), **fields)
    log.info("社区日报 %s 采集=%s 模型=%s", rep["report_id"], cs, ms)
    return rep
