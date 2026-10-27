"""命令行入口:.venv/bin/python -m app.cli <命令>

  worker                       前台启动调度 worker(唯一执行定时任务的进程)
  serve                        启动查询 API 与结果页(不执行定时任务)
  analyze INST [--dry-run] [--print-input]   单次小时分析;dry-run 只看输入规模与摘要
  community [--import FILE]    立即生成一次社区日报(或用导入的帖子)
  probe-community [--pages N]  只验证 Orbit 公开页面采集能力,不调用模型、不落库
  reports [INST] [-n N]        查看最近报告
  jobs [-n N]                  查看任务记录
  watch INST on|off            修改关注列表
  submit INST|daily            向 worker 提交手动任务
  models                       列出 DeepSeek 可用模型(核对 DEEPSEEK_MODEL)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings

# 系统自带 Python 3.9 链接 LibreSSL,urllib3 v2 每次启动都会警告;不影响 HTTPS 请求
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from zoneinfo import ZoneInfo

from app.config import load_settings
from analysis.snapshot import fmt_ts as _fmt_ts

_DISPLAY_TZ = None


def fmt_ts(ms):
    """命令行显示用 RT_DISPLAY_TZ;模型输入固定 UTC+8(与 1D/1W 开盘口径一致)。"""
    return _fmt_ts(ms, _DISPLAY_TZ) if _DISPLAY_TZ else _fmt_ts(ms)


def _setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def summarize_report(r):
    t, f = r["technical"], r["final"]
    return {"report_id": r["report_id"], "inst_id": r["inst_id"],
            "analysis_as_of": fmt_ts(r["analysis_as_of"]), "live_price": r.get("live_price"),
            "final": f["label"], "notes": f.get("notes"),
            "technical": {"verdict": t["verdict"], "right_side": t["right_side"],
                          "structure_confirmed": t["structure_confirmed"], "rr": t["rr"].get("status"),
                          "conflicts": t["conflicts"], "downgrades": t["downgrades"]},
            "model": r.get("model"), "model_status": r["model_status"],
            "community": r["community"].get("status"), "data_quality": t["data_quality"]["status"]}


def main(argv=None):
    p = argparse.ArgumentParser(prog="righttrend", description="OKX 右侧交易分析服务")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker")
    sub.add_parser("serve")
    a = sub.add_parser("analyze")
    a.add_argument("inst")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--print-input", action="store_true")
    c = sub.add_parser("community")
    c.add_argument("--import", dest="import_path")
    pc = sub.add_parser("probe-community")
    pc.add_argument("--pages", type=int, default=3)
    pc.add_argument("--details", type=int, default=5)
    r = sub.add_parser("reports")
    r.add_argument("inst", nargs="?")
    r.add_argument("-n", type=int, default=5)
    r.add_argument("--full", action="store_true")
    j = sub.add_parser("jobs")
    j.add_argument("-n", type=int, default=20)
    w = sub.add_parser("watch")
    w.add_argument("inst")
    w.add_argument("state", choices=["on", "off"])
    sm = sub.add_parser("submit")
    sm.add_argument("target")
    sub.add_parser("models")
    args = p.parse_args(argv)
    _setup_logging()
    s = load_settings()
    global _DISPLAY_TZ
    _DISPLAY_TZ = ZoneInfo(s.display_tz)

    if args.cmd == "worker":
        from jobs.worker import AlreadyRunning, run_worker
        try:
            run_worker(s)
        except AlreadyRunning as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if args.cmd == "serve":
        from app.api import create_app
        create_app(s).run(host=s.web_host, port=s.web_port, debug=False, use_reloader=False)
        return 0
    if args.cmd == "models":
        from llm.deepseek import DeepSeekClient
        _print({"configured_model": s.deepseek_model or None, "available": DeepSeekClient(s).list_models()})
        return 0
    if args.cmd == "probe-community":
        from community.process import make_matcher
        from community.sources import OrbitPublicSource
        src = OrbitPublicSource(s.okx_base, s.community_locale, max_pages=args.pages,
                                max_details=args.details, interval=s.community_request_interval)
        now = int(time.time() * 1000)
        m = make_matcher(["BTC", "ETH"])
        res = src.collect(now - 24 * 3600_000, now, ["BTC", "ETH"], lambda t: m(t))
        with_author = [x for x in res.posts if x.get("author_id")]
        _print({"status": res.status, "error": res.error, "meta": {**res.meta,
                "oldest_seen": fmt_ts(res.meta.get("oldest_seen")),
                "newest_seen": fmt_ts(res.meta.get("newest_seen"))},
                "posts": len(res.posts), "with_author": len(with_author),
                "samples": [{"post_id": x["post_id"], "url": x.get("url"), "time": fmt_ts(x.get("publish_time")),
                             "author": x.get("author_name"), "tags": x.get("cash_tags"),
                             "likes": x.get("likes"), "text": (x.get("text") or "")[:80]}
                            for x in (with_author or res.posts)[:5]]})
        return 0

    from jobs.runner import build_context, community_daily, prepare, analyze
    ctx = build_context(s)
    ctx.store.ensure_watchlist(s.default_watchlist)

    if args.cmd == "analyze":
        inst = args.inst.upper()
        if args.dry_run:
            batch, ev, data, text, meta = prepare(ctx, inst)
            if args.print_input:
                print(text)
                return 0
            _print({"inst_id": inst, "analysis_as_of": fmt_ts(batch.analysis_as_of),
                    "live_price": batch.live_price, "llm": s.llm_status, "input_meta": meta,
                    "evidence_count": {lv: len(ev["levels"][lv]["evidence"]) for lv in ev["levels"]},
                    "active_bsp": [{k: e[k] for k in ("id", "type", "price", "locked", "active", "invalidated")}
                                   for e in ev["index"].values() if e["kind"] == "bsp" and e["active"]],
                    "directions": {lv: ev["levels"][lv]["direction"] for lv in ev["levels"]},
                    "quality": {lv: ev["levels"][lv]["quality"]["status"] for lv in ev["levels"]}})
            return 0
        rep = analyze(ctx, inst)
        _print(summarize_report(rep))
        return 0
    if args.cmd == "community":
        from community.sources import ImportSource
        src = ImportSource.from_file(args.import_path) if args.import_path else None
        rep = community_daily(ctx, source=src)
        out = {k: rep.get(k) for k in ("report_id", "collection_status", "collection_error", "source_mode",
                                       "model", "model_status", "llm_calls", "tokens", "sample_stats",
                                       "validation_errors")}
        out.update(window=[fmt_ts(rep["window_start"]), fmt_ts(rep["window_end"])],
                   expires_at=fmt_ts(rep["expires_at"]),
                   assets=[{k: a.get(k) for k in ("symbol", "data_quality", "sentiment", "trend_expectation",
                                                  "sample", "note")} for a in rep["assets"]])
        _print(out)
        return 0
    if args.cmd == "reports":
        reps = ctx.store.reports(args.inst.upper() if args.inst else None, args.n)
        _print(reps if args.full else [summarize_report(x) for x in reps])
        return 0
    if args.cmd == "jobs":
        _print([{k: (fmt_ts(v) if k.endswith("_at") else v) for k, v in jb.items()}
                for jb in ctx.store.jobs(args.n)])
        return 0
    if args.cmd == "watch":
        ctx.store.set_watch(args.inst.upper(), args.state == "on")
        _print(ctx.store.watchlist_rows())
        return 0
    if args.cmd == "submit":
        t = args.target
        jt, scope = ("manual_daily", "*") if t == "daily" else ("manual_hourly", t.upper())
        jid = ctx.store.claim_job(jt, scope, str(int(time.time() * 1000)), None, status="pending")
        _print({"job_id": jid, "job_type": jt, "scope": scope, "note": "由运行中的 worker 执行"})
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
