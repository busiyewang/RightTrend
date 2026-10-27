"""每日社区日报:采集 → 整理 → DeepSeek → 校验 → 本地映射证据链接 → 带有效期保存。

三种状态严格区分:采集失败(collection_status=failed/blocked/login_required, data_quality=unavailable)、
样本不足(data_quality=insufficient, sentiment=unknown)、社区观点中性(sufficient + sentiment=neutral)。
"""

from __future__ import annotations

import json
import uuid
from typing import Dict, List, Optional

from analysis.schema import validate_community
from analysis.snapshot import fmt_ts
from community.process import make_matcher, process_posts, stratified_sample
from community.sources import CollectionResult, CommunitySource
from llm.deepseek import DeepSeekClient, LLMError
from llm.prompts import COMMUNITY_PROMPT_VERSION, COMMUNITY_SYSTEM_PROMPT

HOUR_MS = 3600_000


def _model_posts(asset: str, posts: List[Dict], text_limit: int, ref_of: Dict[str, str],
                 author_alias: Dict[str, str], counters: Dict) -> List[Dict]:
    out = []
    for p in posts:
        ref = ref_of.setdefault(p["post_id"], f"P{len(ref_of) + 1}")
        a = p.get("author_id")
        alias = author_alias.setdefault(a, f"A{len(author_alias) + 1}") if a else "unknown"
        text = p["text"]
        if len(text) > text_limit:
            counters["truncated"] += 1
            text = text[:text_limit] + "…(截断)"
        item = {"ref": ref, "author": alias, "time": fmt_ts(p["publish_time"]), "text": text}
        for k in ("likes", "comments"):
            if isinstance(p.get(k), int):
                item[k] = p[k]
        if p.get("images"):
            item["images_unparsed"] = p["images"]
        out.append(item)
    return out


def build_report(source: CommunitySource, llm: DeepSeekClient, settings, symbols: List[str],
                 now: int, logical_date: Optional[str] = None,
                 collected: Optional[CollectionResult] = None) -> Dict:
    window_end = now
    window_start = now - settings.community_window_hours * HOUR_MS
    matcher = make_matcher(symbols)
    res = collected or source.collect(window_start, window_end, symbols, lambda t: matcher(t))
    rep: Dict = {
        "report_id": "C" + uuid.uuid4().hex[:15], "logical_date": logical_date,
        "window_start": window_start, "window_end": window_end, "generated_at": now,
        "expires_at": window_end + settings.community_expire_hours * HOUR_MS,
        "collection_status": res.status, "collection_error": res.error,
        "source_mode": res.source_mode, "source": getattr(source, "name", "unknown"),
        "source_meta": res.meta, "symbols": symbols, "prompt_version": COMMUNITY_PROMPT_VERSION,
        "model": None, "model_status": None, "llm_calls": 0, "tokens": 0, "llm_attempts": [],
        "overall": None, "assets": [], "validation_errors": [],
    }
    if res.status in ("failed", "login_required", "blocked"):
        rep["model_status"] = "skipped"
        rep["assets"] = [{"symbol": s, "data_quality": "unavailable", "sentiment": "unknown",
                          "note": f"采集状态 {res.status}: {res.error}"} for s in symbols]
        rep["sample_stats"] = {"raw": 0}
        rep["_posts"] = []
        return rep

    per_asset, stats = process_posts(res.posts, symbols, window_start, window_end, settings)
    counters = {"truncated": 0}
    ref_of: Dict[str, str] = {}
    author_alias: Dict[str, str] = {}
    model_input: Dict[str, List[Dict]] = {}
    sampled: Dict[str, List[Dict]] = {}
    for s in symbols:
        a = per_asset[s]
        if a["data_quality"] != "sufficient":
            continue
        sampled[s] = stratified_sample(a["posts"], settings.community_max_posts_per_asset)
        model_input[s] = _model_posts(s, sampled[s], settings.community_text_limit, ref_of,
                                      author_alias, counters)
    post_by_ref = {ref: pid for pid, ref in ref_of.items()}
    all_posts = {p["post_id"]: p for a in per_asset.values() for p in a["posts"]}
    stats["truncated_for_model"] = counters["truncated"]
    stats["authors_total"] = len({p.get("author_id") for p in all_posts.values() if p.get("author_id")})
    stats["coverage"] = [fmt_ts(window_start), fmt_ts(window_end)]
    rep["sample_stats"] = stats

    model_assets: Dict[str, Dict] = {}
    asset_errors: Dict[str, List[str]] = {}
    sent_refs: Dict[str, set] = {}
    errors = []
    if model_input:
        if not llm.configured:
            rep["model_status"] = "not_configured"
        else:
            # 超预算时按币种分批调用;每批独立校验,ref 编号全局唯一
            payload = {"window": stats["coverage"], "assets": model_input}
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            batches = [list(model_input)] if len(text) <= settings.community_input_max_chars \
                else [[s] for s in model_input]
            for group in batches:
                sub = {"window": stats["coverage"], "assets": {s: model_input[s] for s in group}}
                body = json.dumps(sub, ensure_ascii=False, separators=(",", ":"))
                if len(body) > settings.community_input_max_chars:
                    for s in group:      # 单币种仍超预算:按比例减少帖子,记录裁剪
                        keep = max(10, int(len(sub["assets"][s]) * settings.community_input_max_chars / len(body)))
                        stats.setdefault("model_trim", []).append(f"{s}: 样本 {len(sub['assets'][s])}→{keep} 条")
                        sub["assets"][s] = sub["assets"][s][-keep:]
                    body = json.dumps(sub, ensure_ascii=False, separators=(",", ":"))
                sent_refs.update({s: {p["ref"] for p in sub["assets"][s]} for s in group})
                rep["llm_calls"] += 1
                try:
                    r = llm.chat_json(COMMUNITY_SYSTEM_PROMPT, "社区帖子样本(JSON):\n" + body)
                except LLMError as e:
                    rep["llm_attempts"].extend(e.attempts)
                    errors.append(f"{e.code}: {e.msg}")
                    continue
                rep["llm_attempts"].extend(r.attempts)
                rep["model"] = r.model
                rep["tokens"] += int(r.usage.get("total_tokens") or 0)
                norm, verr = validate_community(r.data, group)
                rep["validation_errors"].extend(verr)
                asset_errors.update(norm.get("asset_errors", {}))
                if norm.get("overall") and (rep["overall"] is None or len(batches) == 1):
                    rep["overall"] = norm["overall"]
                for a in norm.get("assets", []):
                    model_assets[a["symbol"]] = a
            rep["model_status"] = "ok" if model_assets and not errors and not rep["validation_errors"] else \
                ("partial" if model_assets else "failed")
            if errors:
                rep["model_error"] = "; ".join(errors)[:500]
    else:
        rep["model_status"] = "skipped_no_sufficient_asset"

    for s in symbols:
        a = per_asset[s]
        sample = {"valid_posts": a["valid"], "authors": a["authors"],
                  "unknown_author_posts": a["unknown_author_posts"],
                  "author_cap_dropped": a["author_cap_dropped"],
                  "sent_to_model": len(sampled.get(s, [])),
                  "coverage": [fmt_ts(a["coverage"][0]), fmt_ts(a["coverage"][1])] if a["coverage"] else None}
        item = {"symbol": s, "data_quality": a["data_quality"], "sample": sample,
                "sentiment": "unknown", "trend_expectation": None, "themes": [],
                "counterarguments": [], "risks": [], "evidence": [], "analysis_status": "unavailable"}
        if a["data_quality"] == "insufficient":
            item["analysis_status"] = "insufficient"
            item["note"] = (f"样本不足:有效 {a['valid']} 条/{a['authors']} 位作者,"
                            f"门槛 {settings.community_min_posts} 条/{settings.community_min_authors} 位")
        m = model_assets.get(s)
        if a["data_quality"] == "sufficient" and m:
            allowed = sent_refs.get(s, set())
            evs, bad = [], []
            for e in m["evidence"]:
                if e["ref"] not in allowed:
                    bad.append(e["ref"])
                    continue
                p = all_posts[post_by_ref[e["ref"]]]
                evs.append({**e, "post_id": p["post_id"], "url": p.get("url"),
                            "publish_time": p["publish_time"], "author_name": p.get("author_name"),
                            "symbol": s})
            if bad:
                rep["validation_errors"].append(f"{s}: 模型引用了不存在或不属于该币种的帖子 {bad[:5]},已丢弃")
            if not evs:
                item["analysis_status"] = "no_evidence"
                item["note"] = "模型未给出可核对的证据"
                rep["validation_errors"].append(f"{s}: {item['note']},不采纳社区方向")
            elif m["sentiment"] == "unknown":
                item["analysis_status"] = "unknown"
                item["note"] = "社区情绪未知"
                item["evidence"] = evs
            else:
                item.update({k: m[k] for k in ("sentiment", "trend_expectation", "themes",
                                               "counterarguments", "risks")})
                item["evidence"] = evs
                item["analysis_status"] = "ok"
        elif a["data_quality"] == "sufficient":
            if s in asset_errors:
                item["analysis_status"] = "schema_invalid"
                item["validation_errors"] = asset_errors[s]
                item["note"] = "模型输出未通过校验:" + ";".join(asset_errors[s][:3])
            else:
                item["note"] = f"模型结论不可用({rep['model_status']})"
        rep["assets"].append(item)
    if model_input and llm.configured:
        accepted = sum(a["analysis_status"] == "ok" for a in rep["assets"])
        rep["model_status"] = ("failed" if not accepted else
                               "partial" if accepted < len(model_input) or errors or rep["validation_errors"]
                               else "ok")
        if rep["model_status"] == "failed":
            rep["overall"] = None
        if rep["model_status"] != "ok" and "model_error" not in rep:
            rep["model_error"] = ";".join(rep["validation_errors"])[:500] or "模型未给出可用的社区结论"
    rep["_posts"] = list(all_posts.values())
    return rep
