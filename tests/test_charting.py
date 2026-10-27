"""图表接口离线测试:收盘边界、周期口径、缓存与请求隔离。"""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from app.api import create_app
from app.charting import ChartService, ChartUnavailable
from market.okx import OkxError
from market.service import MarketService
from market.timeframes import DURATION_MS, expected_last_closed_open
from storage.db import Store
from tests.helpers import NOW, FakeOkx, series, settings


INST = "ETH-USDT-SWAP"


def make_chart(level="4H", mode="utc8", data=None, **kwargs):
    config = settings(default_watchlist=[INST], daily_bar_mode=mode, warmup_bars=80,
                      min_bars={lv: 20 for lv in DURATION_MS})
    rows = data if data is not None else series(
        level, NOW, 80, offset_ms=config.daily_offset_ms)
    client = FakeOkx({config.okx_bar(level): rows})
    market = MarketService(config, client=client)
    clock = [NOW]
    chart = ChartService(config, market=market, clock=lambda: clock[0], **kwargs)
    return config, client, chart, clock


def test_only_time_closed_confirmed_candles_feed_indicators_and_structure():
    rows = series("4H", NOW, 80)
    # 模拟交易所误标一个当前K线和一个未来K线为已确认。
    rows[-1]["confirm"] = 1
    rows.append(dict(rows[-1], ts=rows[-1]["ts"] + DURATION_MS["4H"]))
    _, _, chart, _ = make_chart(data=rows)
    data = chart.snapshot(INST, "4H")
    assert len(data["candles"]) == 80
    assert all(c["confirm"] == 1 and c["ts"] + DURATION_MS["4H"] <= NOW
               for c in data["candles"])
    assert data["forming_bar"] is None
    assert data["quality"]["excluded_unclosed_confirmed"] == 2
    for group in data["indicators"].values():
        assert all(len(values) == len(data["candles"]) for values in group.values())
    # 索引仍以响应中的完整K线为基准,锁定信息原样保留。
    for stroke in data["chan"]["bi"]:
        assert data["candles"][stroke["start_idx"]]["ts"] == stroke["start_ts"]
        assert data["candles"][stroke["end_idx"]]["ts"] == stroke["end_ts"]
        if stroke.get("locked_at"):
            assert stroke["locked_at"] <= NOW


def test_forming_candle_is_separate_and_does_not_change_last_indicator():
    _, _, chart, _ = make_chart()
    data = chart.snapshot(INST, "4H")
    assert data["forming_bar"]["confirm"] == 0
    assert data["forming_bar"]["ts"] > data["candles"][-1]["ts"]
    assert data["quality"]["confirmed_count"] == 80
    assert len(data["indicators"]["macd"]["hist"]) == 80


@pytest.mark.parametrize("mode", ["utc8", "utc"])
@pytest.mark.parametrize("level", ["1D", "1W"])
def test_daily_weekly_chart_respects_exchange_timezone(mode, level):
    config, client, chart, _ = make_chart(level=level, mode=mode)
    data = chart.snapshot(INST, level)
    assert data["okx_bar"] == config.okx_bar(level)
    assert data["daily_bar_mode"] == mode
    assert data["candles"][-1]["ts"] == expected_last_closed_open(
        level, NOW, config.daily_offset_ms)
    assert data["quality"]["misaligned"] == 0
    assert data["quality"]["status"] == "ok"
    assert {call[1] for call in client.calls} == {config.okx_bar(level)}


def test_cache_refresh_is_lazy_and_cached_payload_cannot_be_mutated():
    _, client, chart, clock = make_chart()
    first = chart.snapshot(INST, "4H")
    first["candles"][0]["c"] = -999
    clock[0] += 59_999
    second = chart.snapshot(INST, "4H")
    assert second["cache_hit"] is True
    assert second["candles"][0]["c"] > 0
    assert len(client.calls) == 1
    clock[0] += 1
    assert chart.snapshot(INST, "4H")["cache_hit"] is False
    assert len(client.calls) == 2
    assert {call[1] for call in client.calls} == {"4H"}


def test_parallel_requests_share_one_fetch():
    _, client, chart, _ = make_chart()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: chart.snapshot(INST, "4H"), range(4)))
    assert len(client.calls) == 1
    assert sum(not r["cache_hit"] for r in results) == 1


def test_fetch_failure_keeps_usable_cache_and_marks_it_stale():
    _, client, chart, clock = make_chart()
    first = chart.snapshot(INST, "4H")
    clock[0] += 61_000
    with patch.object(client, "recent", side_effect=OkxError("temporary failure")):
        data = chart.snapshot(INST, "4H")
    assert data["candles"] == first["candles"]
    assert data["fetched_at"] == NOW
    assert data["analysis_as_of"] == clock[0]
    assert data["quality"]["status"] == "stale"
    assert data["quality"]["refresh_failed"] is True
    assert data["quality"]["cache_fallback"] is True


def test_empty_failure_is_not_cached_and_allows_immediate_retry():
    _, client, chart, _ = make_chart()
    with patch.object(client, "recent", side_effect=OkxError("temporary failure")):
        with pytest.raises(ChartUnavailable) as err:
            chart.snapshot(INST, "4H")
    assert err.value.quality["status"] == "error"
    assert not chart.cache
    assert chart.market.cache.keys() == []
    assert chart.snapshot(INST, "4H")["candles"]


def test_lru_eviction_releases_candles_for_evicted_instrument():
    cache = {}
    _, _, chart, _ = make_chart(max_entries=2, payload_cache=cache)
    for inst in ("A", "B", "C"):
        chart.snapshot(inst, "4H")
    assert len(cache) == 2
    assert set(chart.market.cache.keys()) == {("B", "4H"), ("C", "4H")}


def test_endpoint_rejects_unknown_input_before_fetch_and_never_calls_model():
    config, client, chart, _ = make_chart()
    store = Store(":memory:")
    app = create_app(config, store=store, chart_service=chart)
    web = app.test_client()
    for query in ("inst=../secret", "inst=NOPE-USDT-SWAP", "level=5m", "level=../4H"):
        assert web.get("/api/chart?" + query).status_code == 400
    assert client.calls == []
    with patch("llm.deepseek.DeepSeekClient.chat_json", side_effect=AssertionError("no LLM")):
        response = web.get("/api/chart?inst=eth-usdt-swap&level=4h")
    assert response.status_code == 200
    assert response.json["inst_id"] == INST
    assert response.json["code"] == 0
    assert response.json["candles"]
    assert store.jobs() == []
    assert store.reports() == []


def test_endpoint_empty_response_is_retryable_and_assets_cannot_escape_static_dir():
    config, client, chart, _ = make_chart()
    app = create_app(config, chart_service=chart)
    web = app.test_client()
    with patch.object(client, "recent", side_effect=OkxError("offline")):
        response = web.get("/api/chart")
    assert response.status_code == 503
    assert response.json["code"] == 1
    assert response.json["quality"]["status"] == "error"
    assert web.get("/api/chart").status_code == 200
    assert web.get("/assets/index.html").status_code == 200
    for path in ("../config.py", "%2E%2E/config.py", "../../.env", "/etc/passwd"):
        assert web.get("/assets/" + path).status_code in (404, 308)
