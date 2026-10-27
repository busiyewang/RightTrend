"""查询 API 与中文结果页。Web 不执行定时任务;图表按需抓取公开行情并在内存计算。"""

from __future__ import annotations

import os
import time

from flask import Flask, jsonify, request, send_from_directory

from app.charting import ChartService, ChartUnavailable
from app.config import ROOT, Settings
from storage.db import Store

INST_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _inst(v: str) -> str:
    v = (v or "").upper().strip()
    if not v or len(v) > 40 or not set(v) <= INST_OK:
        raise ValueError("非法币种代码")
    return v


def create_app(settings: Settings, store: Store = None,
               chart_service: ChartService = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    st = store or Store(settings.db_path)
    st.ensure_watchlist(settings.default_watchlist)
    charts = chart_service if chart_service is not None else ChartService(settings)

    @app.get("/api/chart")
    def chart():
        inst = _inst(request.args.get("inst", "ETH-USDT-SWAP"))
        level = request.args.get("level", "4H").strip().upper()
        # 已停用的关注产品仍可查看历史报告旁的图表,但不接受任意币种。
        if inst not in st.watchlist(enabled_only=False):
            raise ValueError("请先将该产品加入关注列表")
        try:
            return jsonify(charts.snapshot(inst, level))
        except ChartUnavailable as e:
            return jsonify({"code": 1, "msg": str(e), "quality": e.quality}), 503

    @app.errorhandler(ValueError)
    def bad(e):
        return jsonify({"code": 1, "msg": str(e)}), 400

    @app.get("/api/status")
    def status():
        lock_pid = None
        try:
            with open(settings.lock_path) as f:
                lock_pid = f.read().strip() or None
        except OSError:
            pass
        return jsonify({
            "code": 0, "now": int(time.time() * 1000),
            "llm": settings.llm_status, "model": settings.deepseek_model or None,
            "schedule": {"hourly_delay_sec": settings.hourly_delay_sec, "daily_time": settings.daily_time,
                         "schedule_tz": settings.schedule_tz, "daily_bar_mode": settings.daily_bar_mode},
            "worker_pid_hint": lock_pid,
            "watchlist": st.watchlist_rows(),
            "recent_jobs": st.jobs(30),
            "community_latest": _creport_brief(st.community_reports(1)),
        })

    @app.get("/api/reports")
    def reports():
        inst = request.args.get("inst")
        n = min(int(request.args.get("n", "24")), 200)
        return jsonify({"code": 0, "reports": st.reports(_inst(inst) if inst else None, n)})

    @app.get("/api/reports/<rid>")
    def report(rid):
        r = st.report(rid)
        return (jsonify({"code": 0, "report": r}) if r else (jsonify({"code": 1, "msg": "not found"}), 404))

    @app.get("/api/community")
    def community():
        n = min(int(request.args.get("n", "5")), 30)
        return jsonify({"code": 0, "reports": st.community_reports(n)})

    @app.get("/api/jobs")
    def jobs():
        return jsonify({"code": 0, "jobs": st.jobs(min(int(request.args.get("n", "50")), 500),
                                                   request.args.get("type"), request.args.get("status"))})

    @app.post("/api/watchlist")
    def watch():
        inst = _inst(request.args.get("inst") or (request.get_json(silent=True) or {}).get("inst"))
        on = (request.args.get("on") or str((request.get_json(silent=True) or {}).get("on", "1"))) in ("1", "true", "True")
        st.set_watch(inst, on)
        return jsonify({"code": 0, "watchlist": st.watchlist_rows()})

    @app.post("/api/run")
    def run():
        target = request.args.get("target") or (request.get_json(silent=True) or {}).get("target")
        if target == "daily":
            jt, scope = "manual_daily", "*"
        else:
            jt, scope = "manual_hourly", _inst(target)
        jid = st.claim_job(jt, scope, str(int(time.time() * 1000)), None, status="pending")
        return jsonify({"code": 0, "job_id": jid, "note": "已提交,由 worker 执行;worker 未运行时任务保持 pending"})

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(ROOT, "app", "static"), "index.html")

    @app.get("/assets/<path:filename>")
    def asset(filename):
        return send_from_directory(os.path.join(ROOT, "app", "static"), filename)

    return app


def _creport_brief(reps):
    if not reps:
        return None
    r = reps[0]
    return {k: r.get(k) for k in ("report_id", "generated_at", "expires_at", "collection_status",
                                  "source_mode", "model_status")} | {
        "assets": [{k: a.get(k) for k in ("symbol", "data_quality", "sentiment")} for a in r["assets"]]}
