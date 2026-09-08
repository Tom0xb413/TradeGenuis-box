# 高位旗形 / 杯柄（柄）

看板「形态」可选 `pattern_family`：

| 值 | 看板 | 行为 |
|---|---|---|
| `box` | 箱体/通道 | **默认**。沿用 classic / p0 / p1 箱体四条件，不改 100 分制 |
| `high_flag` | 高位旗形(杯柄) | 跑 `pattern_flag.detect_high_flag`；列表按旗面 / 二次买点筛选 |
| `trendline` | 趋势线 | 跑 `pattern_trendline.detect_trendline`；见 [pattern-trendline.md](pattern-trendline.md) |

写入 `data/config.json`，经 `GET/POST /api/config` 持久化。A 股全市场 / 快扫 / 自选池与币圈扫描、`/api/kline` 都读该字段。

切换形态族后：**1 小时扫描缓存按 `pattern_family`（以及 `box_mode`）区分身份**，不会把箱体结果当成旗形结果吐出。K 线叠加按当前形态现算（缓存的 bars 不重拉）。卡片列表仍是上一次扫描的结果，需 **强制重扫**。形态与扫描结果不一致时横幅提示强制重扫（与箱体模式 mismatch 相同）。

`family=box` 时箱体模式胶囊照常工作。`family=high_flag` 时箱体模式变淡禁用，并出现「全部旗形 / 仅旗形 / 仅二次买点」筛选。

## 与教科书形态的关系

| 名称 | 对应 |
|---|---|
| Bull Flag / 高位旗形 | 放量 pole（旗杆）+ 高位浅回撤缩量旗面 |
| Cup-Handle 的柄 | 不识别完整杯体，只识别「突破后高位收窄的柄」 |
| 突破中继 | 强势突破后不深调，整理后再选二次买点 |

本检测器**不是**完整杯柄（不要求左侧圆弧底），也不是箱体上沿试盘。四条件 100 分制在 `high_flag` 下不作为入选门槛；扫描行仍带箱体字段与评分，供对照，UI 按旗形字段过滤。

## Pole（旗杆 / t_break）

在最近 `POLE_LOOKBACK=40` 根内从新到旧搜索。硬条件（都要满足）：

| 参数 | 定稿 | 含义 |
|---|---|---|
| `POLE_BODY_PCT` | **5%** | `(close−open)/open ≥ 5%` **或** `(close−昨收)/昨收 ≥ 5%` |
| `POLE_VOL_MULT` | **2.0** | 量能 ≥ 2.0 × 前 20 根均量（不含 pole；口径同箱体 MA20） |

相对昨收这条覆盖 A 股**一字涨停**（实体可为 0，涨幅仍约 10%/20%）。不单开涨停板百分比表。

**累计涨幅（可选、已定稿为软条件）**：`pole_gain` = 含 pole 在内最近最多 5 根的最低价到 pole 收盘。**不作为入选硬门槛**，避免把「单日放量突破、基数不够 15%」挡掉。`pole_gain ≥ 15%` 且旗面更紧/更缩量时 `flag_quality=strong`。

## 旗面 / 柄（pole 之后 K ∈ [2, 15]）

整段窗口必须同时满足：

| 参数 | 定稿 | 含义 |
|---|---|---|
| 浅回撤 | `HANDLE_DEPTH=8%` | `min(Low) ≥ min(pole 中轴, Close_pole×0.92)`。中轴 = `Low + 0.5×(High−Low)`。两条门槛取 **OR**（较松的那条） |
| 缩量 | `HANDLE_VOL_DRY=0.55` | `mean(Volume) ≤ 0.55 × Volume_pole` |
| 收窄 | `HANDLE_RANGE_MAX=8%` | `(maxHigh−minLow)/minLow < 8%` |

软条件（只影响 `flag_quality`，不否决）：

- 旗面均实体 `< 0.4 × pole 实体`（`HANDLE_BODY_RATIO`）
- `min(Volume_handle) <` pole 处 MA20

同一根 pole：若最后一根已满足二次买点，则旗面**不含**该根（避免突破日把振幅撑爆）。

## 二次买点（当前 K）

在有效旗面之后的**最新一根**：

- `Close > max(High_handle)`
- `Volume > 1.4 × mean(Volume_handle)`（`SECONDARY_VOL_MULT=1.40`）

历史二次突破（不是最新一根）不标 `secondary_buy`。

## 输出字段

| 字段 | 值 |
|---|---|
| `pattern` | `high_flag` \| `none` |
| `flag_stage` | `handle` \| `secondary_buy` \| `none` |
| `pole_date` / `pole_gain` / `handle_len` | 日期、累计涨幅%、旗面根数 |
| `vol_dry_ratio` | 旗面均量 / pole 量 |
| `retrace_depth` | 相对 pole 收盘的回撤 %（向下为正，向上钳为 0） |
| `handle_high` / `handle_low` | 旗面最高 / 最低 |
| `flag_quality` | `strong` \| `ok` |
| `is_handle_consolidation` | 当前处于旗面（非二次买点） |
| `secondary_breakout_buy` | 最新一根为二次买点 |
| `pole_index` / `handle_start` / `handle_end` | K 线叠加用下标 |
| `secondary_break_level` | 等于 `handle_high` |

K 线：pole 柱高亮、旗面价格带、二次突破水平虚线。悬浮提示写 Pole / 旗面 / 二次买点。

## 扫描与缓存

- 检测在 `fetch_kline` / 币圈 K 线之后对每只标的运行（A 股市场/快扫/自选池、币圈同一引擎）
- 载荷带 `pattern_family`；`scan_cache_response` 要求家庭 + 箱体模式都与当前配置一致
- CI 只跑合成 K 线与配置/缓存契约，**不跑全市场扫描**
