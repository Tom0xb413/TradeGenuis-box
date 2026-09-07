# 箱体识别模式

看板与扫描共用同一套引擎（`box_engine.py`）。选中的模式写入 `data/config.json` 的 `box_mode`，经 `GET/POST /api/config` 持久化。A 股 / 币圈扫描与 `/api/kline` 都读该字段。

| mode | 看板 | 状态 |
|---|---|---|
| `classic` | 经典 | **默认**，行为与本 fork 原 `compute_box` 一致 |
| `p0` | P0增强 | 分位数水平箱 + 振幅门控 |
| `p1` | P1通道 | **斜向通道**：OLS 中轴 + 残差分位带 + 温和斜率 / R² / ADX |

切换模式后：K 线叠加会按当前模式现算（`/api/kline` 用缓存 bars 重算 box）；评分卡片仍是上一次扫描的结果，需 **强制重扫**。模式与扫描结果 `box_mode` 不一致时，1 小时扫描缓存不会命中。

四条件 100 分制**不因模式改权重**：P0/P1 只改变箱体几何与 `tests` 计数，评分仍用 `tests≥3` 等原规则。

## classic（默认）

- 窗口：最近 60 根；若最近 15 根内收盘有效突破「前 40 根最高价 × 1.005」，窗口截止到突破日（不含突破 K）。
- 边界：`box_high = max(High)`，`box_low = min(Low)`。
- 试盘（需同时满足）：
  - `High >= box_high × 0.985`（`BOX_NEAR`）
  - `Close <= box_high × 1.005`（`BOX_CLOSE`）
  - `Volume >= 0.70 × 窗口均量`（`TEST_VOL`）
  - 上影占比 `>= 0.30`（`BOX_SHADOW`），**或** `High >= box_high × 0.995`

## p0（定稿参数）

突破截断与 classic **相同**（仍用前 40 根 raw max 判断是否已突破，不用分位数），避免把「刚放量突破」算进箱体。

| 参数 | 定稿 | 含义 |
|---|---|---|
| `P0_AMP_MAX` | **0.15（15%）** | `(box_high - box_low) / mean(close)` ≥ 15% → 不成箱，`tests=0`，`box_quality=amplitude_reject` |
| `P0_HIGH_Q` | 0.95 | `box_high = quantile(High, 0.95)`（线性插值，口径同 numpy `linear`） |
| `P0_LOW_Q` | 0.05 | `box_low = quantile(Low, 0.05)` |
| `P0_CLOSE_MULT` | 1.005 | 收盘须回到上沿内侧（`Close <= edge × 1.005`） |
| `P0_SHADOW_BODY` | 2.0 | 上影线 ≥ 2× 实体 |
| `P0_SHADOW_RANGE` | 0.40 | 上影线 / (H−L) ≥ 0.4 |
| `P0_TEST_VOL_MULT` | 1.8 | 现量 ≥ 1.8 × 前 20 根均量（不含当前；前序不足 5 根则用可得样本） |
| `P0_POST_HOLD_BARS` | 3 | **软校验**：随后 1–3 根收盘站上中轴 → `post_hold_*` 元数据，**不否决**试盘、不参与评分 |

试盘硬条件：High 触及或刺穿上沿（`High >= box_high`，无 classic 的 0.985 宽松带）+ 上列收盘 / 上影 / 量能。

`span_pct` 仍按 `(high-low)/low×100` 写入扫描行的 `box_span_pct`（与 classic 卡片字段兼容）。门控用的振幅另存在 `amp_pct`。

`box_quality=amplitude_reject` 时看板**不画**水平箱线，角标「非箱体·振幅过大」。

## p1（斜向通道，定稿）

上升/下降通道 = 线性回归中轴 + 残差分位数平行带。P1 **不是**无趋势震荡过滤器：温和斜向通道是目标形态。

### 窗口

**复用** `select_box_window` 的突破截断（与 classic/p0 相同：近 15 根内收盘 > 前 40 最高 × 1.005 则窗口止于突破日前），再取专用回看 **`P1_LOOK = 50`** 根（落在 30–60 建议区间中段，比 classic 的 60 略短，让斜率估计更贴近近端）。窗口短于 **`P1_MIN_BARS = 30`** 则不成通道（返回 `None`）。

不另做一套截断，避免刚放量突破的 K 被拟合进通道。

### 拟合

1. 窗口 Close 对 `t = 0..n-1` 做 OLS → 截距 `β0`、斜率 `β1`，中轴 `mid(t) = β1·t + β0`。
2. 残差：`High_t - mid(t)`、`Low_t - mid(t)`。
3. `B_up = quantile(high_resid, 0.95)`，`B_down = quantile(low_resid, 0.05)`（线性插值，与 P0 相同）。
4. 第 i 根（全局下标，`t = i - start`）：`R(i) = mid(i) + B_up`，`S(i) = mid(i) + B_down`。窗口之后的 K **外推**同一条斜线，供突破事件分类。
5. 卡片 `box_high` / `box_low` = **全序列最后一根** 的 R/S。
6. 看板绘制用 `channel_points[]`：`{date, mid, up, down}`，从窗口起点到最后一根。

分位数残差对单根毛刺比 `max(High-mid)` 稳健。

### 门控（定稿）

先算几何，再按下列顺序取**第一个**失败项；失败则 `tests=0`，`box_quality` 如下，看板不画通道（与 P0 振幅拒绝同一 UX）。

| 参数 | 定稿 | 规则 |
|---|---|---|
| `P1_WIDTH_MAX` | **0.25（25%）** | `(R−S)/mid`（用最后一根 mid）≥ 25% → `amplitude_reject` |
| `P1_SLOPE_NORM_MAX` | **0.004 / 根** | `slope_norm = β1 / mean(Close)`，绝对值 > 0.004 → `slope_reject`（过陡） |
| `P1_SLOPE_NORM_MIN` | **0.0005 / 根** | `slope_norm ∈ [0.0005, 0.004]` → `box_kind=ascending`；对称负区间 → `descending`；`|slope_norm| < 0.0005` → `flat_fallback`（**不硬拒**，仍受 R²/穿越/ADX 约束） |
| `P1_R2_MIN` / `P1_R2_MAX` | **[0.35, 0.80]** | Close~t 的 R² 落在区间外 → `r2_reject`（过噪或近直线单边趋势） |
| `P1_MIN_MID_CROSSES` | **3** | Close−mid 符号变化（跳过贴轴 0）< 3 → `vshape_reject`（V 形一折而非通道震荡） |
| `P1_ADX_PERIOD` / `P1_ADX_MAX` | **14 / 40** | 在**完整 K 线**上算 Wilder ADX(14)，取窗口最后一根。`ADX > 40` → `adx_reject`（过猛趋势）。**ADX 20–40 放行**，以保留温和上升/下降通道；ADX 算不出时不因 ADX 拒绝 |

未触发拒绝时：`box_quality` 仍按试盘次数 `ok` / `weak` / `none`（≥3 / ≥1 / 0），与 P0 相同，供卡片提示。

### 试盘与上沿事件

- **tests**（评分用）：相对**该根** `R(t)`，规则与 P0 同精神——`High ≥ R(t)`，`Close ≤ R(t)×1.005`，上影 ≥ 2×实体且占振幅 ≥ 0.4，量 ≥ 1.8×MA20。
- **edge_events**：`classify_edge_events` 接受 per-bar `R(t)`；突破确认/失败时，t+1 / t+2 的收盘与**当时**的 R 比较，而不是钉死候选日的 R。

### 返回字段（在原有 box 上）

`box_mode=p1`，`box_kind`（`ascending` \| `descending` \| `flat_fallback`），`slope`，`slope_norm`，`r2`，`adx`，`b_up` / `b_down`，`width_pct`，`mid_crosses`，`channel_points`，以及既有 `box_high` / `box_low` / `tests` / `edge_events`。

扫描行（`box_row_fields`）带上 `box_kind` / `slope_norm` / `r2` / `adx`，不带 `channel_points`（只在 `/api/kline` 的 box 里，避免扫描 JSON 膨胀）。

## 上沿事件：试盘 vs 真突破

相对**当前模式**算出的上沿给刺穿 K 分类：classic/p0 为水平 `R`，p1 为 `R(t)`。与各模式自己的 `tests` 计分**并行**，四条件 100 分制**不改**。

触发前提：`High >= R`（或该根 `R(t)`）。

| `kind` | 含义 | 定稿规则 |
|---|---|---|
| `test` | 试盘 / 假突破 | `Close ≤ R×1.005`，上影/(H−L)≥**0.4**，上影≥**2×**实体，量≥**1.8×**MA20 |
| `breakout_candidate` | 潜在突破 | `Close ≥ R×1.015`（α=**1.5%**），实体/振幅≥**0.60**，上影/振幅≤**0.15**，量≥**1.8×**MA20 |
| `breakout_confirmed` | 确认真突破 | 先满足 candidate，且随后 **2** 根收盘仍 **> 当时 R**。**事后标签**，需要 t+2 已收盘；最新一根不会标 confirmed |
| `breakout_failed` | 失败突破 | candidate 但下一根收盘 **< 当时 R**（或 t+2 未能站上） |
| `none` | 未归类 | 未刺穿，或收盘落在 `R×1.005` 与 `R×1.015` 之间，或形态/量能不够 |

字段（箱体 / 扫描行 / `/api/kline` 的 `box`）：

- `edge_event`：最后一根 K 的分类
- `last_edge_event` / `last_edge_event_date`：窗口起最近一次非 none 事件
- `edge_events`：最近最多 12 条 `{date, kind, index}`
- `breakout_candidates`：candidate+confirmed+failed 根数
- `breakout_confirmed` / `breakout_failed`：计数
- `tests`：仍是该模式自己的试盘计数（评分用）

看板：最新一根为试盘/突破时卡片小徽章；K 线悬浮提示该根若在 `edge_events` 里会写出中文标签。P1 有 `channel_points` 时画斜向三线（上轨/中轴/下轨）；门控拒绝时不画箱/通道，脚注「非箱体·振幅过大」等。
