"""配置:环境变量优先,其次项目根目录 .env(只读,不回写)。

所有可调项集中在 Settings;核心模块只接收 Settings 实例,不直接读环境。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_dotenv(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _list(v: str) -> List[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    # 行情
    okx_base: str = "https://www.okx.com"
    default_watchlist: List[str] = field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    daily_bar_mode: str = "utc8"          # utc8 → 1D/1W(OKX 默认 UTC+8 开盘);utc → 1Dutc/1Wutc
    cache_max_bars: int = 1200
    warmup_bars: int = 1200
    min_bars: Dict[str, int] = field(default_factory=lambda: {"1H": 120, "4H": 100, "1D": 60, "1W": 20})
    required_levels: List[str] = field(default_factory=lambda: ["1H", "4H", "1D"])
    model_bars: Dict[str, int] = field(default_factory=lambda: {"1H": 72, "4H": 60, "1D": 60, "1W": 52})
    http_timeout: float = 15.0

    # 调度
    hourly_delay_sec: int = 90
    daily_time: str = "09:10"
    schedule_tz: str = "America/Los_Angeles"
    display_tz: str = "Asia/Shanghai"

    # 策略 / 风控
    min_net_rr: float = 2.0
    fee_rate: float = 0.0005              # 单边手续费率(taker 假设)
    slippage_rate: float = 0.0005         # 单边滑点假设
    stop_buffer: float = 0.002
    price_ref_tolerance: float = 0.003    # 模型价格与引用关键位允许偏差

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_model: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_json_mode: bool = True
    deepseek_timeout: float = 180.0
    deepseek_max_retries: int = 2
    deepseek_max_tokens: int = 4000
    deepseek_temperature: float = 0.2
    tech_input_max_chars: int = 90000
    community_input_max_chars: int = 60000

    # 社区
    community_source: str = "orbit_public"
    community_locale: str = "zh-hans"
    community_window_hours: int = 24
    community_expire_hours: int = 26
    community_max_pages: int = 60
    community_max_details: int = 300
    community_request_interval: float = 1.2
    community_min_posts: int = 10
    community_min_authors: int = 5
    community_author_cap: int = 3
    community_max_posts_per_asset: int = 60
    community_text_limit: int = 600

    # 存储 / 服务
    db_path: str = os.path.join(ROOT, "var", "righttrend.db")
    lock_path: str = os.path.join(ROOT, "var", "worker.lock")
    web_host: str = "127.0.0.1"
    web_port: int = 8430

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_model)

    @property
    def llm_status(self) -> str:
        if not self.deepseek_api_key:
            return "not_configured"
        if not self.deepseek_model:
            return "not_configured(缺少 DEEPSEEK_MODEL)"
        return "configured"

    def okx_bar(self, level: str) -> str:
        """内部级别名 → OKX bar 参数。1D/1W 在 utc 模式下整体切换为 1Dutc/1Wutc。"""
        if level in ("1D", "1W") and self.daily_bar_mode == "utc":
            return level + "utc"
        return level

    @property
    def daily_offset_ms(self) -> int:
        return 0 if self.daily_bar_mode == "utc" else 8 * 3600_000


def load_settings(env_file: Optional[str] = None, **overrides) -> Settings:
    env = _read_dotenv(env_file or os.path.join(ROOT, ".env"))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("RT_", "DEEPSEEK_"))})

    def g(name: str, default=None):
        v = env.get(name, "").strip()
        return v if v else default

    s = Settings()
    s.deepseek_api_key = g("DEEPSEEK_API_KEY", "")
    s.deepseek_model = g("DEEPSEEK_MODEL", "")
    s.deepseek_base_url = g("DEEPSEEK_BASE_URL", s.deepseek_base_url).rstrip("/")
    s.deepseek_json_mode = g("DEEPSEEK_JSON_MODE", "1") != "0"
    for name, attr, conv in [
        ("DEEPSEEK_TIMEOUT", "deepseek_timeout", float),
        ("DEEPSEEK_MAX_RETRIES", "deepseek_max_retries", int),
        ("DEEPSEEK_MAX_TOKENS", "deepseek_max_tokens", int),
        ("RT_TECH_INPUT_MAX_CHARS", "tech_input_max_chars", int),
        ("RT_COMMUNITY_INPUT_MAX_CHARS", "community_input_max_chars", int),
        ("RT_HOURLY_DELAY_SEC", "hourly_delay_sec", int),
        ("RT_MIN_NET_RR", "min_net_rr", float),
        ("RT_FEE_RATE", "fee_rate", float),
        ("RT_SLIPPAGE_RATE", "slippage_rate", float),
        ("RT_COMMUNITY_MAX_PAGES", "community_max_pages", int),
        ("RT_COMMUNITY_MAX_DETAILS", "community_max_details", int),
        ("RT_COMMUNITY_MIN_POSTS", "community_min_posts", int),
        ("RT_COMMUNITY_MIN_AUTHORS", "community_min_authors", int),
        ("RT_COMMUNITY_WINDOW_HOURS", "community_window_hours", int),
        ("RT_COMMUNITY_EXPIRE_HOURS", "community_expire_hours", int),
        ("RT_WEB_PORT", "web_port", int),
    ]:
        v = g(name)
        if v is not None:
            setattr(s, attr, conv(v))
    for name, attr in [("RT_DAILY_TIME", "daily_time"), ("RT_SCHEDULE_TZ", "schedule_tz"),
                       ("RT_DISPLAY_TZ", "display_tz"), ("RT_DAILY_BAR_MODE", "daily_bar_mode"),
                       ("RT_COMMUNITY_SOURCE", "community_source"), ("RT_DB_PATH", "db_path"),
                       ("RT_OKX_BASE", "okx_base"), ("RT_WEB_HOST", "web_host")]:
        v = g(name)
        if v is not None:
            setattr(s, attr, v)
    v = g("RT_WATCHLIST")
    if v:
        s.default_watchlist = [x.upper() for x in _list(v)]
    if s.daily_bar_mode not in ("utc8", "utc"):
        raise ValueError("RT_DAILY_BAR_MODE 只能是 utc8 或 utc")
    for k, val in overrides.items():
        setattr(s, k, val)
    return s
