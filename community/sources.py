"""社区来源适配器。

OrbitPublicSource 只读取公开网页里服务端渲染的 appState JSON(浏览器打开页面即可看到),
不调用隐藏接口、不登录、不绕过限制:
- 列表:/{locale}/orbit/sitemap/insight/{page} —— 公开站点地图,按发布时间倒序,每页约 50 条,
  每条含 contentId、发布时间、正文(可能截断)、链接;不含作者与互动数。
  注意:这是"站点地图收录的帖子",官方未说明是否等于全站全量。
- 详情:/{locale}/orbit/post/{id} —— 作者、完整正文、币种标签、图片、点赞/评论/浏览数。
  只对提到关注币种的帖子抓详情,数量有上限。
遇到 403/429/验证码返回 blocked,跳转登录返回 login_required,结构变化返回 failed。

ImportSource:读取人工导入的真实帖子 JSON,用于在自动采集不可用时测试分析链路。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
APPSTATE_RE = re.compile(r'id="appState">(.*?)</script>', re.S)
PAGE_RETRIES = 2
RETRY_BACKOFF = 3.0
MAX_CONSECUTIVE_PAGE_FAILURES = 3
LIST_LAG_TOLERANCE_MS = 3600_000
CHALLENGE_MARKERS = ("captcha", "cf-challenge", "challenge-platform", "verify you are human",
                     "access denied")


@dataclass
class CollectionResult:
    status: str                         # ok / partial / failed / login_required / blocked
    source_mode: str                    # public_web / import
    posts: List[Dict] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)
    error: Optional[str] = None


class CommunitySource:
    name = "base"
    source_mode = "public_web"

    def collect(self, window_start: int, window_end: int, symbols: List[str],
                matcher: Callable[[str], List[str]]) -> CollectionResult:
        raise NotImplementedError


class PageError(Exception):
    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status


def _ms(v) -> Optional[int]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 1e12 else v * 1000)
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n if n > 1e12 else n * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:          # 无时区的时间无法确认,不猜
        return None
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


class OrbitPublicSource(CommunitySource):
    name = "orbit_public"
    source_mode = "public_web"

    def __init__(self, base: str = "https://www.okx.com", locale: str = "zh-hans",
                 max_pages: int = 60, max_details: int = 300, interval: float = 1.2,
                 timeout: float = 20.0, session: Optional[requests.Session] = None,
                 sleep=time.sleep):
        self.base = base.rstrip("/")
        self.locale = locale
        self.max_pages = max_pages
        self.max_details = max_details
        self.interval = interval
        self.timeout = timeout
        self.sess = session or requests.Session()
        self.sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
        self.sleep = sleep
        self.requests = 0

    def _page(self, path: str) -> Dict:
        if self.requests:
            self.sleep(self.interval)
        self.requests += 1
        try:
            r = self.sess.get(f"{self.base}/{self.locale}/{path}", timeout=self.timeout)
        except requests.RequestException as e:
            raise PageError("failed", f"网络错误: {e}")
        final = r.url.split("?")[0].rstrip("/")
        if "/account/login" in final or final.endswith("/login"):
            raise PageError("login_required", "页面跳转到登录")
        if r.status_code in (403, 429):
            raise PageError("blocked", f"HTTP {r.status_code}")
        if r.status_code == 404:
            raise PageError("not_found", "HTTP 404")
        if r.status_code != 200:
            raise PageError("failed", f"HTTP {r.status_code}")
        m = APPSTATE_RE.search(r.text)
        if not m:
            low = r.text[:20000].lower()
            if any(k in low for k in CHALLENGE_MARKERS):
                raise PageError("blocked", "出现验证/拦截页面")
            raise PageError("failed", "页面中没有 appState(结构可能已变化)")
        try:
            state = json.loads(m.group(1))
            return (state["appContext"].get("initialProps") or {}).get("orbit") or {}
        except (ValueError, KeyError, TypeError) as e:
            raise PageError("failed", f"appState 解析失败: {e}")

    def _retry(self, fn):
        """服务端渲染偶尔缺数据(实测 sitemap 第 7 页一次缺失、稍后正常):failed 类错误退避重试。
        blocked / login_required / not_found 不重试。"""
        for i in range(PAGE_RETRIES + 1):
            try:
                return fn()
            except PageError as e:
                if e.status != "failed" or i == PAGE_RETRIES:
                    raise
                self.sleep(RETRY_BACKOFF * (i + 1))

    def list_page(self, n: int) -> Dict:
        def get():
            o = self._page(f"orbit/sitemap/insight/{n}")
            sd = o.get("sitemapInsightData")
            if not isinstance(sd, dict) or not isinstance(sd.get("posts"), list):
                raise PageError("failed", f"第 {n} 页 sitemapInsightData 缺失")
            return sd
        return self._retry(get)

    def detail(self, post_id: str) -> Dict:
        def get():
            d = self._page(f"orbit/post/{post_id}").get("detail")
            if not isinstance(d, dict):
                raise PageError("failed", "detail 缺失")
            return d
        return self._retry(get)

    @staticmethod
    def parse_detail(d: Dict) -> Dict:
        items = d.get("contentList") or []
        c = items[0] if items else {}
        tags = (c.get("tagList") or {}).get("cashTagList") or []
        ref = c.get("referencedContent") or {}
        author = d.get("author") or {}
        return {
            "author_id": author.get("authorId") or None,
            "author_name": author.get("nickName") or None,
            "official": author.get("officialStatus") not in (None, "", "0"),
            "text": c.get("content") or "",
            "publish_time": _ms(c.get("publishTime")),
            "cash_tags": sorted({t.get("coinName") for t in tags if t.get("coinName")}),
            "images": len(c.get("imageList") or []),
            "likes": d.get("likeCount"), "comments": d.get("commentNum"),
            "views": d.get("viewCount"),
            "quoted_id": (ref.get("contentId") if isinstance(ref, dict) else None) or None,
        }

    def collect(self, window_start, window_end, symbols, matcher) -> CollectionResult:
        meta = {"entry": f"{self.base}/{self.locale}/orbit/sitemap/insight/{{page}}",
                "method": "公开网页服务端渲染 JSON(appState),逐页翻页 + 详情页补作者/标签/互动",
                "pages_fetched": 0, "details_fetched": 0, "details_failed": 0,
                "details_skipped_cap": 0, "total_pages": None, "oldest_seen": None,
                "newest_seen": None, "reached_window_start": False,
                "coverage_notes": ["列表来自 Orbit 公开站点地图(insight),官方未说明是否覆盖全部帖子",
                                   "图片内容未解析", "推荐流(首页 feed)不作为样本来源"]}
        stubs: Dict[str, Dict] = {}
        status = "ok"
        err = None
        page = 1
        fails = 0
        meta["pages_failed"] = []
        while page <= self.max_pages:
            try:
                sd = self.list_page(page)
                fails = 0
            except PageError as e:
                err = str(e)
                if e.status in ("blocked", "login_required"):
                    status = e.status if not meta["pages_fetched"] else "partial"
                    break
                meta["pages_failed"].append(page)
                fails += 1
                if not meta["pages_fetched"] and fails >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    status = "failed"
                    break
                if fails >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    status = "partial"
                    break
                page += 1            # 跳过这一页(留洞并记录),继续向更早翻
                continue
            meta["pages_fetched"] += 1
            meta["total_pages"] = sd.get("totalPages")
            posts = sd["posts"]
            if not posts:
                meta["reached_window_start"] = True
                break
            all_old = True
            for p in posts:
                t = _ms(p.get("publishTime"))
                pid = str(p.get("contentId") or "")
                if t is not None:
                    meta["oldest_seen"] = t if meta["oldest_seen"] is None else min(meta["oldest_seen"], t)
                    meta["newest_seen"] = t if meta["newest_seen"] is None else max(meta["newest_seen"], t)
                    if t >= window_start:
                        all_old = False
                else:
                    all_old = False   # 没有时间的帖子不能作为停止依据
                if not pid or pid in stubs:
                    continue
                stubs[pid] = {"post_id": pid, "source": self.name, "url": p.get("url"),
                              "publish_time": t, "text": p.get("content") or "",
                              "text_source": "sitemap", "author_id": None, "author_name": None,
                              "cash_tags": [], "images": None, "likes": None, "comments": None,
                              "views": None, "quoted_id": None, "status": "list_only"}
            # 整页都早于窗口起点才停止(不是遇到一条旧帖就停)
            if all_old:
                meta["reached_window_start"] = True
                break
            if not sd.get("hasNext", True) or (sd.get("totalPages") and page >= sd["totalPages"]):
                meta["reached_window_start"] = True
                break
            page += 1
        if status == "ok" and meta["pages_failed"]:
            status = "partial"
            err = f"第 {meta['pages_failed']} 页重试后仍失败,已跳过: {err}"
        if status == "ok" and not meta["reached_window_start"]:
            status = "partial"
            err = err or f"达到翻页上限 {self.max_pages} 页仍未覆盖到窗口起点"
        # 实测(2026-10-27)站点地图最新帖子比当前时间晚约 8 小时:窗口末端这段没有覆盖,必须明说
        if meta["newest_seen"] is not None:
            lag = window_end - meta["newest_seen"]
            meta["list_lag_hours"] = round(lag / 3600_000, 2)
            if lag > LIST_LAG_TOLERANCE_MS:
                note = f"站点地图最新帖子落后窗口终点 {meta['list_lag_hours']} 小时,该时段未覆盖"
                meta["coverage_notes"].append(note)
                if status == "ok":
                    status = "partial"
                    err = note
        if status in ("failed", "login_required", "blocked"):
            return CollectionResult(status, self.source_mode, [], meta, err)

        in_window = [s for s in stubs.values()
                     if s["publish_time"] is None or window_start <= s["publish_time"] <= window_end]
        wanted = [s for s in in_window if matcher(s["text"])]
        wanted.sort(key=lambda s: s["publish_time"] or 0)
        meta["matched_list_posts"] = len(wanted)
        if len(wanted) > self.max_details:
            # 超过详情上限时在整个时间窗口内等间隔抽取,避免只覆盖最近几小时
            step = len(wanted) / self.max_details
            picked = {int(i * step) for i in range(self.max_details)}
            meta["details_skipped_cap"] = len(wanted) - len(picked)
            wanted = [w for i, w in enumerate(wanted) if i in picked]
        for s in wanted:
            try:
                d = self.parse_detail(self.detail(s["post_id"]))
            except PageError as e:
                meta["details_failed"] += 1
                s["status"] = "detail_failed"
                if e.status in ("blocked", "login_required"):
                    err = f"详情页 {e.status}: {e}"
                    status = "partial"
                    meta["details_aborted"] = len(wanted) - (meta["details_fetched"] + meta["details_failed"])
                    break
                continue
            meta["details_fetched"] += 1
            sitemap_time = s["publish_time"]
            s.update({k: v for k, v in d.items() if v not in (None, "")})
            if sitemap_time and not d.get("publish_time"):
                s["publish_time"] = sitemap_time
            s["text_source"] = "detail"
            s["status"] = "ok"
        if meta["details_failed"] or meta["details_skipped_cap"] or meta.get("details_aborted"):
            status = "partial" if status == "ok" else status
            if meta["details_skipped_cap"] and not err:
                err = (f"详情抓取达到上限 {self.max_details},{meta['details_skipped_cap']} 条相关帖子"
                       f"未抓详情(缺作者),未进入样本")
        meta["requests"] = self.requests
        return CollectionResult(status, self.source_mode, list(stubs.values()), meta, err)


class ImportSource(CommunitySource):
    """人工导入真实帖子:JSON 数组,或 {"posts": [...]}。字段见 README。"""
    name = "import"
    source_mode = "import"

    def __init__(self, posts: List[Dict], origin: str = "manual"):
        self.raw = posts
        self.origin = origin

    @classmethod
    def from_file(cls, path: str) -> "ImportSource":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        posts = data.get("posts") if isinstance(data, dict) else data
        if not isinstance(posts, list):
            raise ValueError("导入文件必须是帖子数组或 {\"posts\": [...]}")
        return cls(posts, origin=path)

    def collect(self, window_start, window_end, symbols, matcher) -> CollectionResult:
        out = []
        for i, p in enumerate(self.raw):
            if not isinstance(p, dict):
                continue
            pid = p.get("post_id") or p.get("id")
            out.append({
                "post_id": str(pid) if pid else None, "source": "import",
                "url": p.get("url") or None,
                "author_id": p.get("author_id") or p.get("author") or None,
                "author_name": p.get("author_name") or None,
                "publish_time": _ms(p.get("publish_time")),
                "text": p.get("text") or p.get("content") or "",
                "text_source": "import",
                "cash_tags": [s.upper() for s in (p.get("symbols") or p.get("tags") or []) if isinstance(s, str)],
                "images": p.get("images") if isinstance(p.get("images"), int) else len(p.get("images") or []),
                "likes": p.get("likes"), "comments": p.get("comments"), "views": p.get("views"),
                "quoted_id": p.get("quoted_id"), "status": "ok",
            })
        meta = {"entry": self.origin, "method": "人工导入 JSON", "raw_records": len(self.raw),
                "coverage_notes": ["人工导入样本,覆盖范围取决于导入者"]}
        return CollectionResult("ok", self.source_mode, out, meta)
