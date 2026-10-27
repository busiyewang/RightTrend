# RightTrend · OKX 右侧交易分析服务

每小时把同一币种 **1H / 4H / 1D / 1W** 四个周期的已收盘 K 线和本地缠论证据交给 DeepSeek,判断是否出现
**右侧交易机会**；每天采集 **OKX Orbit(星球)公开社区帖子**,交给 DeepSeek 做社区趋势日报。
两条结论由本地规则校验后汇总。本服务只做分析,不下单。

> 结论只是分析参考,不构成投资建议。模型给出的 confidence 是主观把握,不是历史胜率或收益概率。

---

## 1. 快速开始

```bash
cd /Users/wangye/learnCode/RightTrend
/usr/bin/python3 -m venv .venv                 # 本机 Python 3.9.6;代码兼容 3.9
.venv/bin/pip install -r requirements.txt
cp .env.example .env                           # 填 DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL
.venv/bin/python -m app.cli models             # 列出 DeepSeek 当前可用模型 ID,核对后写入 DEEPSEEK_MODEL
```

| 用途 | 命令 |
| --- | --- |
| 前台启动调度 worker(唯一执行定时任务的进程) | `.venv/bin/python -m app.cli worker` |
| 停止 worker | 前台按 `Ctrl+C`;或 `kill $(cat var/worker.lock)`(SIGTERM,执行中的任务结束后退出) |
| 启动查询 API + 中文结果页 | `.venv/bin/python -m app.cli serve` → http://127.0.0.1:8430 |
| 单次分析(立即执行、落库) | `.venv/bin/python -m app.cli analyze BTC-USDT-SWAP` |
| 只看模型输入规模与摘要(不调用模型、不落库) | `.venv/bin/python -m app.cli analyze BTC-USDT-SWAP --dry-run` |
| 立即生成社区日报 | `.venv/bin/python -m app.cli community` |
| 用导入的真实帖子生成日报 | `.venv/bin/python -m app.cli community --import posts.json` |
| 只验证 Orbit 采集能力(不调模型、不落库) | `.venv/bin/python -m app.cli probe-community --pages 3 --details 5` |
| 查看最近报告 / 完整报告 | `.venv/bin/python -m app.cli reports BTC-USDT-SWAP -n 5 [--full]` |
| 查看任务记录 | `.venv/bin/python -m app.cli jobs -n 30` |
| 关注列表 | `.venv/bin/python -m app.cli watch SOL-USDT-SWAP on` / `off` |
| 向运行中的 worker 提交手动任务 | `.venv/bin/python -m app.cli submit BTC-USDT-SWAP` / `submit daily` |
| 离线测试 | `.venv/bin/python -m pytest -q` |

没有 DeepSeek key 也能运行:行情、缓存、本地证据和社区采集照常工作,模型状态显示 `not_configured`,
小时报告的最终结论为"无有效技术结论"。

本机代理提示(沿用参考项目经验):访问 OKX 需要走系统代理(不要清 `https_proxy`);用 curl 访问本服务时加
`--noproxy '*'`,例如 `curl -s --noproxy '*' http://127.0.0.1:8430/api/status`。

### 可选:后台常驻(launchd)

服务关闭后不会执行任何任务。需要常驻时可自行创建 `~/Library/LaunchAgents/com.righttrend.worker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.righttrend.worker</string>
  <key>WorkingDirectory</key><string>/Users/wangye/learnCode/RightTrend</string>
  <key>ProgramArguments</key><array>
    <string>/Users/wangye/learnCode/RightTrend/.venv/bin/python</string>
    <string>-m</string><string>app.cli</string><string>worker</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>https_proxy</key><string>http://127.0.0.1:29290</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/Users/wangye/learnCode/RightTrend/var/worker.log</string>
</dict></plist>
```

`launchctl load ~/Library/LaunchAgents/com.righttrend.worker.plist` 启用,`launchctl unload …` 停用。
**仓库没有替你启用常驻服务。** worker 有文件锁,重复启动会直接退出,不会重复执行。

---

## 2. 两条分析流程

```
每小时(整点后 90s,UTC 整点)            每天 09:10(America/Los_Angeles,可配)
  增量更新 4 周期K线(内存)                 采集 Orbit 公开帖子(最近 24h)
  同一批次不可变快照                        去重 / 广告过滤 / 币种归类 / 作者上限
  本地缠论 + 指标 → 带编号的证据和关键位      DeepSeek 社区日报 → 校验 → 本地映射链接
  DeepSeek 技术分析(JSON)                 保存日报,window_end + 26h 过期
  Schema 校验 + 本地规则复核 ──────┬──────────┘
                                  汇总(确定性规则,不调第三次模型)→ 小时报告
```

### 2.1 行情与内存缓存(`market/`)

- OKX 公共接口:`/api/v5/market/candles`(最近,≤300 根)与 `/api/v5/market/history-candles`(更早,≤100 根/页)。
- 缓存键 `(inst_id, level)`,每键最多 1200 根,按开盘时间去重排序;同一根以新数据覆盖,
  已收盘K线不会被迟到的未收盘版本回退。**原始K线只在进程内存**,重启后重新预热。
- 首次预热分页到 1200 根;之后增量:只取缺口 + 3 根重叠;长时间离线时继续向前分页补缺口。
- 每周期质量检查:最后已收盘K线是否到达应收盘边界(4H/1D/1W 各按自己的边界,周线周中不会被误判过期)、
  缺口、开盘口径对齐、根数是否达到最低分析根数(1H 120 / 4H 100 / 1D 60 / 1W 20)。
  新币不补零、不伪造周线;1H/4H/1D 任一不足或过期 → 本小时结论"数据不足"。1W 不足只作警告。
- 1D/1W 默认 OKX 的 UTC+8 开盘口径;`RT_DAILY_BAR_MODE=utc` 时整体切换为 `1Dutc/1Wutc`,不混用。

### 2.2 本地证据(`analysis/`)

- `analysis/chanlun/` 是参考项目 `chan*.py / indicators.py / decision.py` 的副本(仅改为包内相对引用,
  算法与参数不变,版本 `chan-b23-v4-macd-10-20-5`),上游的 52 个单元测试一并迁移。
- 只用 `confirm=1` 的K线计算结构;未收盘K线单独放在 `forming_bars_unconfirmed`,只作风险参考。
- 证据编号:`E-1H-BSP-<ts>-B3`、`E-4H-ZS-<ts>`、`E-1D-BC-…`、`E-1H-BI-…`;关键位编号:
  `L-4H-ZG`、`L-1H-BSP-<ts>-B3`、`L-1D-RECENT_HIGH`、`LIVE`(现价)。每条信号带 `known_at / locked_at /
  active / invalidated`。
- 模型输入:1H 72 / 4H 60 / 1D 60 / 1W 52 根紧凑表 + 证据 + 关键位 + 缺失说明,实测约 5.7 万字符。
  超过 `RT_TECH_INPUT_MAX_CHARS`(默认 9 万)时先删辅助说明,再按比例缩短历史,四个周期始终保留,
  裁剪记录写进 `input_meta.trim`;仍超预算则不调用模型。

### 2.3 右侧判定与本地复核(`analysis/validate.py`)

模型结论只会被降级、不会被升级。依次检查:

1. 数据质量:必需周期过期/不足 → `insufficient_data`。
2. 模型失败、空响应、截断、非 JSON、未知枚举、缺字段、方向与信号类型矛盾 → `unavailable`(无有效技术结论)。
3. 引用了不存在的证据编号 → 视为编造证据 → `watch`。
4. `right_side=confirmed` 必须引用一个 **已锁定、锁定与可知时间都不晚于分析时刻、仍活跃、未被价格破坏** 的
   B2/B3(多)或 S2/S3(空);B1/S1、背离、超买超卖不能确认。未通过 → 降为 `pending`。
5. 4H 或 1D 结构方向与交易方向相反 → 标注冲突 → `watch`。
6. 入场/止损/目标必须引用关键位 id(可带百分比偏移),本地核对价格与引用一致、顺序正确、现价未越过止损;
   本地按 **现价** 重新计算净盈亏比(入场+出场双边手续费与滑点,默认各 0.05%),≥ 2.0 才算"现价适合参与"。
   缺少价格或成本假设时 `rr.status=not_checked`,绝不显示"已通过"。
7. "右侧结构已确认"(`structure_confirmed`)与"现价适合参与"(`price_ok`)分开输出。

### 2.4 汇总规则(`analysis/combine.py`)

| 技术结论 | 社区结论 | 最终 |
| --- | --- | --- |
| long/short(已确认且现价通过) | 同向 | 技术候选 · 社区同向 |
| long/short | 反向 | 分歧 → 观望(不反转方向) |
| long/short | 中性/分歧 | 技术候选,注明社区态度 |
| long/short | 不足/失败/过期/缺失 | 技术候选,标注"社区参考不可用" |
| watch(待确认/无信号) | 任意 | 观望 |
| 数据不足 / 模型失败 | 任意 | 无有效技术结论 |

小时报告记录实际引用的日报 `community_report_id`;之后生成的日报不会回填旧报告。

---

## 3. 社区采集:已验证范围与限制

OKX 官方 FAQ 说明 Orbit 没有公开 API。本项目只读取**公开网页中服务端渲染的 appState JSON**(浏览器打开页面
就能看到的数据),不登录、不调用隐藏接口、不绕过访问限制。

2026-10-27 实测(本机网络,经系统代理):

| 入口 | 结论 |
| --- | --- |
| `/{locale}/orbit`(首页 feed) | SSR 只带 10 条推荐帖,`cursor` 参数对 SSR 无效 → **不作样本来源**(推荐流不是全站样本) |
| `/{locale}/orbit/sitemap/insight/{n}` | 公开站点地图,按发布时间倒序,每页 50 条,约 987 页;含 `contentId`、`publishTime`(ms)、正文(可能截断)、链接;**不含作者与互动数**。完整运行时 20 页覆盖到 24h 窗口起点(1000 条) |
| `/{locale}/orbit/post/{id}` | 详情:作者 ID/昵称、完整正文、`$BTC` 等币种标签、图片、点赞/评论/浏览数、引用的原帖 |
| `/{locale}/orbit/sitemap/post/{n}` | 404,不存在 |

采集流程(`community/sources.py`):翻 sitemap 直到**整页**都早于窗口起点(不会因一条旧帖就停止);
对提到关注币种的帖子抓详情(默认上限 300 条/天、间隔 1.2s,超过上限时在 24 小时内等间隔抽取);
没抓到详情的帖子缺作者,不进入样本,计入 `no_detail`。403/429/验证页 → `blocked`;跳转登录 → `login_required`;
页面无 appState → `failed`(结构可能变化);翻页上限内没覆盖完整窗口或详情未抓全 → `partial`。

**尚存的限制(未解决):**

- **站点地图滞后约 8 小时**:实测最新一条的发布时间比当前时间早 8.0 小时(两次测量一致)。"最近 24 小时"窗口里
  最新约 8 小时的帖子不在列表中,无法从公开页面枚举。采集器会测出 `list_lag_hours`,写进 `coverage_notes`,
  并把 `collection_status` 标为 `partial`;窗口本身不会被悄悄平移。
- 服务端渲染偶尔缺数据(实测第 7 页一次没有 `sitemapInsightData`,几分钟后恢复正常):每页最多重试 2 次(间隔 3s/6s),
  仍失败就跳过该页并记入 `pages_failed`,本次标为 `partial`;连续 3 页失败才停止翻页。
- sitemap(insight)是否等于全站全部帖子,官方没有说明;报告里 `coverage_notes` 会写明。
- 依赖页面 SSR 结构,OKX 改版后会返回 `failed`,需要更新解析。目前只验证了单次运行,**没有验证多日无人值守的稳定性**。
- 图片型观点不解析,计入 `unparsed_images` / `image_only`。
- 默认详情上限下,大部分相关帖子只用于计数,不进入模型样本。
- 无公开 API,不承诺长期稳定。自动采集不可用时,可用 `community --import` 导入真实帖子 JSON 继续测试分析链路:

```json
[{"post_id": "91134890438272", "url": "https://www.okx.com/orbit/post/91134890438272",
  "author_id": "872846026913558529", "author_name": "…", "publish_time": 1793066116461,
  "text": "…", "symbols": ["BTC"], "likes": 3, "comments": 0, "images": 1, "quoted_id": null}]
```

`publish_time` 可以是毫秒时间戳或带时区的 ISO 时间;无时区或缺失 → 列入"发布时间无法确认",不计入样本;
缺 `post_id` → 不能生成引用,丢弃。

整理规则(`community/process.py`):ID 去重 → 短转发归并到原帖 → 正文 4-gram Jaccard ≥ 0.85 近似去重 →
广告关键词过滤 → 币种归类(`$BTC` 标签、代码与常用别名,`ETHFI` 不会命中 `ETH`;未知标签不映射合约)→
单作者每币种最多 3 条 → 按作者轮转取样(不按互动数)。每币种至少 10 条有效帖、5 位作者才是 `sufficient`。

日报状态区分:采集失败(`collection_status` ≠ ok/partial,`data_quality=unavailable`)/ 样本不足
(`insufficient`,`sentiment=unknown`,不调模型)/ 社区观点中性(`sufficient` + `neutral`)。
模型引用不存在或不属于该币种的帖子编号会被丢弃并记入 `validation_errors`;链接一律由本地映射。

---

## 4. 调度、去重与运维

- worker 是唯一执行定时任务的进程(文件锁 `var/worker.lock`);`serve` 只读状态、改关注列表、提交手动任务。
- 小时任务唯一键 `(hourly, inst_id, UTC 整点)`;每日任务唯一键 `(daily, *, 调度时区逻辑日期)`;SQLite 唯一约束
  防止重复 tick、重启或多次调用造成重复执行。
- 10:00:30 启动 → 10:01:30 执行 10 点任务。某整点只在它自己那一小时内有效;停机期间错过的时段记
  `skipped/missed`(最多回溯 48 小时),**不补发**收费请求;重启后只补当前有效时段。
  每日任务:最近一个已到期的逻辑日在下一次到期前都可补跑,更早的记 missed。夏令时由 zoneinfo 处理,已测 2026-11-01 与 2027-03-14。
- 任务状态 `pending / running / succeeded / failed / skipped`,记录计划时间、开始结束时间、错误码、模型、
  提示词版本、实际调用次数和 token。进程在任务中途退出 → 重启时标为 `failed / interrupted / uncertain=1`,不自动重跑。
- DeepSeek:JSON 模式 + 本地 Schema 校验;429/5xx/网络错误有限重试(默认 2 次),尊重 `Retry-After`;
  读超时记为"不确定"(可能已计费)。基线调用量 = 每天 `24 × 币种数 + 1`,分批、重试、手动任务会增加。
- 单币种失败、社区采集失败、tick 异常都不会终止调度循环。
- SQLite(`var/righttrend.db`)保存关注列表、任务、报告、社区日报与帖子来源记录;**不保存原始K线数组、API key、Cookie**。
  普通日志只记录结论与状态。

### 排查

| 现象 | 处理 |
| --- | --- |
| 报告 `model_status=not_configured` | `.env` 缺 `DEEPSEEK_API_KEY` 或 `DEEPSEEK_MODEL` |
| `http_401` / `http_400 model not exist` | key 错误或模型 ID 不可用,运行 `app.cli models` 核对 |
| `data_quality.blocking` 显示 stale | OKX 未返回最新收盘K线或抓取失败(检查代理);`app.cli analyze X --dry-run` 看各周期质量 |
| 日报 `blocked / login_required` | 页面被限制或要求登录;在浏览器中正常访问确认后再试,或改用 `--import` |
| 日报 `failed: 页面中没有 appState` | OKX 页面结构变化,需要更新 `community/sources.py` |
| 手动任务一直 pending | worker 没在运行 |
| `已有 worker 在运行` | 已有进程持有锁;`cat var/worker.lock` 看 pid |

---

## 5. 代码结构

```
app/        config.py(配置)  cli.py(命令行)  api.py(查询 API)  static/index.html(结果页)
market/     okx.py(OKX 客户端)  cache.py(内存缓存)  service.py(预热/增量/质量/批次快照)  timeframes.py
analysis/   chanlun/(移植的缠论与指标)  evidence.py  snapshot.py  schema.py  validate.py  combine.py
community/  sources.py(Orbit 公开页 / 导入)  process.py(整理)  report.py(日报)
llm/        deepseek.py  prompts.py
jobs/       runner.py(单次任务)  scheduler.py(可注入时钟)  worker.py(单实例循环)
storage/    db.py(SQLite)
tests/      离线夹具与测试(不访问网络、不调用付费 API)
```

API:`GET /api/status`、`GET /api/reports?inst=&n=`、`GET /api/reports/<id>`、`GET /api/community?n=`、
`GET /api/jobs`、`POST /api/watchlist?inst=&on=1|0`、`POST /api/run?target=<inst>|daily`。

与参考项目的差异:新项目有独立虚拟环境与配置,不 import 参考项目的 `server.py`,不复制其 `.env`;
修正了旧实现每小时强制全量重拉、缓存无过期检查、模型输出只检查能否解析 JSON、硬编码模型名与 `reasoner`
判断、定时器依赖 `__main__` 启动等问题。

---

## 6. 验证状态(2026-10-27)

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 离线测试 | ✅ 117 通过 / 1 跳过 | `pytest -q`;跳过的一项是上游需要外部行情夹具的审计测试 |
| OKX 实际连通 | ✅ 已验证 | BTC/ETH 四周期预热各 1200 根(BTC 1W 实际 362 根,历史到底);增量更新、质量检查全部 `ok`;模型输入约 5.7 万字符,无裁剪 |
| worker 实际运行 | ✅ 已验证一次 | 03:14 PDT 启动 → 补跑 2026-10-26 日报 → 执行 10:00Z 两个币种的小时任务 → 文件锁拒绝第二个 worker → SIGTERM 正常退出 |
| 查询 API / 结果页 | ✅ 已验证 | `/api/status`、`/` 返回 200;非法币种代码返回 400 |
| 社区单次自动采集 | ⚠️ 部分可用 | 单次完整运行:20 页列表、300 次详情(失败 3)、BTC 246 帖 / 184 作者、ETH 168 帖 / 129 作者,均为 `sufficient`;因详情上限和约 8 小时的列表滞后,状态为 `partial` |
| 社区持续每日采集 | ❌ 未验证 | 只跑过单次;多日无人值守的稳定性未测 |
| DeepSeek 实际调用 | ❌ 未验证 | 本项目没有配置 key(按要求没有复制参考项目的 `.env`);模型路径只在离线测试里用假响应验证。填好 `.env` 后先运行 `app.cli models`,再运行 `app.cli analyze BTC-USDT-SWAP` |
| 常驻后台服务 | ❌ 未启用 | 只提供了 launchd 示例 |
