"""帖子整理:时间筛选、去重(ID / 转发 / 正文近似)、广告过滤、币种归类、作者上限、分层取样。"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

ALIASES = {
    "BTC": ["比特币", "大饼"],
    "ETH": ["以太坊", "以太", "二饼"],
    "SOL": ["索拉纳"],
    "DOGE": ["狗狗币"],
    "XRP": ["瑞波"],
}
AD_PATTERNS = [r"邀请码", r"返佣", r"注册链接", r"扫码", r"加群", r"进群", r"私信我", r"免费带单",
               r"t\.me/", r"telegram", r"vx[::]", r"微信[::]", r"领取空投", r"推荐码", r"referral"]
AD_RE = re.compile("|".join(AD_PATTERNS), re.I)
NEAR_DUP_THRESHOLD = 0.85
SHINGLE = 4


def base_symbol(inst_id: str) -> str:
    return inst_id.split("-")[0].upper()


def make_matcher(symbols: List[str]):
    pats = {}
    for s in symbols:
        words = [re.escape(s)] + [re.escape(a) for a in ALIASES.get(s, [])]
        # 英文代码两侧不能紧挨英文字母/数字,避免 ETHFI 命中 ETH
        pats[s] = re.compile(r"(?<![A-Za-z0-9])\$?(?:" + "|".join(words) + r")(?![A-Za-z0-9])", re.I)

    def match(text: str, tags: Optional[List[str]] = None) -> List[str]:
        hit = {s for s in symbols if s in (tags or [])}
        for s, p in pats.items():
            if p.search(text or ""):
                hit.add(s)
        return sorted(hit)
    return match


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _shingles(text: str) -> set:
    t = _norm(text)
    if len(t) <= SHINGLE:
        return {t} if t else set()
    return {t[i:i + SHINGLE] for i in range(len(t) - SHINGLE + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def process_posts(posts: List[Dict], symbols: List[str], window_start: int, window_end: int,
                  settings) -> Tuple[Dict[str, List[Dict]], Dict]:
    matcher = make_matcher(symbols)
    st = {"raw": len(posts), "no_id": 0, "untimed": 0, "out_of_window": 0, "ads": 0,
          "dup_id": 0, "dup_repost": 0, "dup_near": 0, "image_only": 0, "unparsed_images": 0,
          "no_symbol": 0, "no_detail": 0, "valid": 0, "untimed_samples": []}
    seen_ids = set()
    kept: List[Dict] = []
    for p in sorted(posts, key=lambda x: x.get("publish_time") or 0):
        pid = p.get("post_id")
        if not pid:
            st["no_id"] += 1                     # 无稳定标识:不能生成引用
            continue
        if pid in seen_ids:
            st["dup_id"] += 1
            continue
        seen_ids.add(pid)
        if p.get("publish_time") is None:
            st["untimed"] += 1
            if len(st["untimed_samples"]) < 20:
                st["untimed_samples"].append({"post_id": pid, "url": p.get("url")})
            continue
        if not (window_start <= p["publish_time"] <= window_end):
            st["out_of_window"] += 1
            continue
        text = (p.get("text") or "").strip()
        if (p.get("images") or 0) > 0:
            if not text:
                st["image_only"] += 1
                continue
            st["unparsed_images"] += 1
        if not text:
            continue
        if AD_RE.search(text):
            st["ads"] += 1
            continue
        syms = matcher(text, p.get("cash_tags"))
        if not syms:
            st["no_symbol"] += 1             # 与关注币种无关,不进入后续去重和样本
            continue
        if p.get("status") in ("list_only", "detail_failed"):
            st["no_detail"] += 1             # 只有列表摘要、缺作者:无法做作者集中度检查,不进样本
            continue
        kept.append(dict(p, text=text, symbols=syms))

    # 转发:正文很短且引用了别的帖子 → 归并到原帖;原帖已在样本中则视为重复
    by_id = {p["post_id"]: p for p in kept}
    reposts_of = set()
    deduped = []
    for p in kept:
        q = p.get("quoted_id")
        if q and len(_norm(p["text"])) < 30:
            if q in by_id or q in reposts_of:
                st["dup_repost"] += 1
                continue
            reposts_of.add(q)
        deduped.append(p)

    # 正文近似去重(保留最早一条);同一作者跨币种的同文帖也会被合并
    final: List[Dict] = []
    sh_list: List[set] = []
    for p in deduped:
        sh = _shingles(p["text"])
        if any(_jaccard(sh, o) >= NEAR_DUP_THRESHOLD for o in sh_list):
            st["dup_near"] += 1
            continue
        sh_list.append(sh)
        final.append(p)

    groups: Dict[str, List[Dict]] = defaultdict(list)
    for p in final:
        for s in p["symbols"]:
            groups[s].append(p)

    per_asset = {}
    for s in symbols:
        items = groups.get(s, [])
        per_author: Dict[str, int] = defaultdict(int)
        capped, dropped = [], 0
        for p in items:
            a = p.get("author_id")
            if a:
                if per_author[a] >= settings.community_author_cap:
                    dropped += 1
                    continue
                per_author[a] += 1
            capped.append(p)
        authors = {p["author_id"] for p in capped if p.get("author_id")}
        unknown_author = sum(1 for p in capped if not p.get("author_id"))
        sufficient = (len(capped) >= settings.community_min_posts
                      and len(authors) >= settings.community_min_authors)
        times = [p["publish_time"] for p in capped]
        per_asset[s] = {
            "posts": capped, "valid": len(capped), "authors": len(authors),
            "unknown_author_posts": unknown_author, "author_cap_dropped": dropped,
            "coverage": [min(times), max(times)] if times else None,
            "data_quality": "sufficient" if sufficient else "insufficient",
        }
    st["valid"] = len(final)
    return per_asset, st


def stratified_sample(posts: List[Dict], limit: int) -> List[Dict]:
    """按作者轮转取样(每轮每个作者取最新一条),避免少数作者主导;不按互动数排序。"""
    by_author: Dict[str, List[Dict]] = defaultdict(list)
    for p in sorted(posts, key=lambda x: x["publish_time"], reverse=True):
        by_author[p.get("author_id") or f"_unknown_{p['post_id']}"].append(p)
    queues = list(by_author.values())
    out: List[Dict] = []
    while queues and len(out) < limit:
        nxt = []
        for q in queues:
            if len(out) >= limit:
                break
            out.append(q.pop(0))
            if q:
                nxt.append(q)
        queues = nxt
    return sorted(out, key=lambda x: x["publish_time"])
