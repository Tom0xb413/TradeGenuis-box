# 趋势线（摆动高低点）

看板「形态」可选 `pattern_family`：

| 值 | 看板 | 行为 |
|---|---|---|
| `box` | 箱体/通道 | **默认**。沿用 classic / p0 / p1 箱体四条件，不改 100 分制 |
| `high_flag` | 高位旗形(杯柄) | 跑 `pattern_flag.detect_high_flag`；见 [pattern-high-flag.md](pattern-high-flag.md) |
| `trendline` | 趋势线 | 跑 `pattern_trendline.detect_trendline`；列表按「有线 / 刚跌破支撑 / 刚突破压力」筛选 |

写入 `data/config.json`，经 `GET/POST /api/config` 持久化。A 股全市场 / 快扫 / 自选池与币圈扫描、`/api/kline` 都读该字段。

切换形态族后：**1 小时扫描缓存按 `pattern_family`（以及 `box_mode`）区分身份**，不会把箱体结果当成趋势线结果吐出。K 线叠加按当前形态现算（缓存的 bars 不重拉）。卡片列表仍是上一次扫描的结果，需 **强制重扫**。形态与扫描结果不一致时横幅提示强制重扫。

`family=box` 时箱体模式胶囊照常工作。`family=trendline`（以及 `high_flag`）时箱体模式隐藏，并出现趋势线事件筛选。

## 与 Tom 图例的关系

手工趋势线的核心不是「水平箱」，而是把摆动点连成一条 **价格 × 时间** 的斜线 L(t)。价格在到期边界这一侧还有效；**该根收盘越过 L(t)**，结构就切换——他用「期权到期」作类比。

Phase 1 只做：

| 名称 | 对应 |
|---|---|
| 上升支撑 | 最近一段更高低（higher lows）连线 |
| 下降压力 | 最近一段更低高（lower highs）连线 |
| 跌破 / 突破 | 最新 K 的 Close 带 α 缓冲越过 L(t) |

Phase 2（三角形 / 楔形）本版只留枚举：`tl_pattern` 可为 `triangle`，**检测恒输出 `none`**，不参与筛选。

本检测器**不是**箱体上沿试盘，也不是 P1 残差通道。四条件 100 分制在 `trendline` 下不作为入选门槛；扫描行仍带箱体字段与评分，供对照，UI 按 `tl_*` 过滤。

## 摆动点

窗口 **k = 3**（建议区间 3–5 的下沿，合成样本更干净）。下标 i 为局部高当且仅当

`High[i] == max(High[j], j ∈ [i−k, i+k])`（闭区间，左右各 k 根，含自身共 2k+1）。局部低对 Low 同理。

两端不足 k 根不标。间隔 ≤ k 的同侧点合并为更极端者。拟合只用最近 `LOOKBACK=80` 根上、每侧最多 10 个摆动点。

## 拟合

在同侧摆动点中两两连线：

`L(t) = p1 + (p2 − p1) / (t2 − t1) × (t − t1)`

t 为 K 线整数下标。

| 参数 | 定稿 | 含义 |
|---|---|---|
| `MIN_SPAN_BARS` | **8** | 两锚点至少相隔 8 根 |
| `MIN_RISE` | **0.2%** | 支撑必须更高低，压力必须更低高（排除水平箱） |
| `MAX_SLOPE_NORM` | **0.02 / 根** | `\|slope\| / 均价` 过陡则丢弃 |
| `TOUCH_TOL` | **0.6%** | 其它摆动点落在 L(t) 的 0.6% 内计为触点（0.3%–1% 中段） |
| `MIN_TOUCHES` | **2** | 两端算触点；≥3 为 `quality=ok`，2 为 `weak` |
| `PIERCE_TOL` | **0.8%** | 两锚点之间 Close 越过 L 达 0.8% 计一次重刺穿 |
| `MAX_MID_PIERCE` | **2** | 中段重刺穿超过 2 根 → 否决该线 |
| 陈旧突破 | 第二锚点之后、最近 3 根之前 | 若已有 Close 越过 α，视为旧结构，丢弃 |

评分键：触点多优先，其次第二锚点更新，再次跨度更长。支撑与压力独立各留一条最佳。

## 越线事件（定稿：收盘 + α，不要求放量）

优先用 **Close**，与「该根时间戳到期」一致；影线刺穿但收盘未越过不算结构改变。

| 事件 | 规则 |
|---|---|
| `support_break` | 前一根 Close ≥ L(t−1)，当前 Close < L(t)×(1−α) |
| `resistance_break` | 前一根 Close ≤ L(t−1)，当前 Close > L(t)×(1+α) |

**α = `BREACH_ALPHA` = 0.3%**（0.2%–0.5% 中段）。只在最近 `EVENT_LOOK=3` 根上标注，故筛选文案为「刚」跌破 / 突破。

未采用的备选：连续 2 根收盘确认、实体穿越、放量。本版为及时性（对应到期边界）选择单根 Close+α；假突破需结合仓位自行过滤。

## 输出字段

扫描行（`trendline_row_fields`，不含点列）：

| 字段 | 值 |
|---|---|
| `tl_support` / `tl_resistance` | `{side, t1, p1, t2, p2, date1, date2, slope, touches, L_now, quality}` 或 `null` |
| `tl_event` | `support_break` \| `resistance_break` \| `none` |
| `tl_event_bar` | 事件 K 的日期 |
| `tl_quality` | `ok` \| `weak` \| `none`（任一线 `ok` 则总体 `ok`） |
| `tl_pattern` | 恒 `none`（Phase 2 预留 `triangle`） |
| `tl_has_line` | 至少一条有效线 |

`/api/kline` 另带完整 `trendline` 对象，含 `tl_points.support/resistance`：锚点 1、锚点 2、最后一根的 `{date, price}`，供看板画斜线。扫描 JSON 不带 `tl_points`。

K 线：绿色虚线支撑、珊瑚色虚线压力，越线 K 用竖向色带标出。悬浮提示写跌破支撑 / 突破压力 / 锚点。

## 扫描与缓存

- 检测在 `fetch_kline` / 币圈 K 线之后对每只标的运行（与旗形一样始终打标，UI 按形态族过滤）
- 载荷带 `pattern_family`；`scan_cache_response` 要求家庭 + 箱体模式都与当前配置一致
- CI 只跑合成 K 线与配置/缓存契约，**不跑全市场扫描**
