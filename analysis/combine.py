"""技术结论 + 社区日报的确定性汇总(不再调用模型)。社区只能降级,不能升级或反转技术方向。"""

from __future__ import annotations

from typing import Dict, Optional

ALIGN = {"long": "bullish", "short": "bearish"}
OPPOSE = {"long": "bearish", "short": "bullish"}


def community_view(report: Optional[Dict], symbol: str, as_of: int) -> Dict:
    """取当前有效日报中该币种的结论;过期、失败、样本不足都标为不可用并注明原因。"""
    if not report:
        return {"available": False, "status": "missing", "note": "暂无社区日报"}
    base = {"report_id": report["report_id"], "generated_at": report["generated_at"],
            "expires_at": report["expires_at"]}
    if report["expires_at"] <= as_of:
        return {**base, "available": False, "status": "expired", "note": "社区日报已过期"}
    if report["generated_at"] > as_of:
        return {**base, "available": False, "status": "future", "note": "日报晚于分析时刻"}
    if report["collection_status"] in ("failed", "login_required", "blocked"):
        return {**base, "available": False, "status": report["collection_status"],
                "note": f"社区采集状态 {report['collection_status']}"}
    asset = next((a for a in report.get("assets") or [] if a.get("symbol") == symbol), None)
    if not asset:
        return {**base, "available": False, "status": "unavailable", "note": f"日报未覆盖 {symbol}"}
    if asset.get("data_quality") != "sufficient":
        return {**base, "available": False, "status": asset.get("data_quality"),
                "note": f"{symbol} 社区样本{'不足' if asset.get('data_quality') == 'insufficient' else '不可用'}"}
    if asset.get("sentiment") in (None, "unknown"):
        return {**base, "available": False, "status": "unknown", "note": "社区情绪未知"}
    return {**base, "available": True, "status": "ok", "sentiment": asset["sentiment"],
            "trend_expectation": asset.get("trend_expectation"),
            "sample": asset.get("sample")}


def combine(tech: Dict, comm: Dict, downgrade_on_conflict: bool = True) -> Dict:
    tv = tech["verdict"]
    notes = []
    if tv in ("insufficient_data", "unavailable"):
        return {"final": "no_signal", "label": "无有效技术结论",
                "notes": ["技术数据不足或模型失败,社区情绪不能补成交易信号"]}
    if tv == "watch":
        label = "观望"
        if comm.get("available"):
            notes.append(f"社区{_cn(comm['sentiment'])},但技术面未确认,保持观望")
        return {"final": "watch", "label": label, "notes": notes}
    # tv ∈ long/short:右侧已确认且现价条件通过
    side_cn = "做多" if tv == "long" else "做空"
    if not comm.get("available"):
        return {"final": tv, "label": f"技术候选:{side_cn}",
                "notes": [f"社区参考不可用({comm.get('note')})"]}
    s = comm["sentiment"]
    if s == ALIGN[tv]:
        return {"final": tv, "label": f"技术候选:{side_cn} · 社区同向", "notes": []}
    if s == OPPOSE[tv]:
        if downgrade_on_conflict:
            return {"final": "watch", "label": "技术与社区分歧 → 观望",
                    "notes": [f"技术面{side_cn}已确认,但社区{_cn(s)};按规则降为观望,不反转方向"]}
        return {"final": tv, "label": f"技术候选:{side_cn} · 社区分歧", "notes": []}
    return {"final": tv, "label": f"技术候选:{side_cn}", "notes": [f"社区{_cn(s)}"]}


def _cn(s: str) -> str:
    return {"bullish": "看多", "bearish": "看空", "mixed": "分歧", "neutral": "中性",
            "unknown": "未知"}.get(s, s)
