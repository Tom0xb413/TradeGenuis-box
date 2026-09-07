# 箱体识别模式

看板与扫描共用同一套引擎（`box_engine.py`）。选中的模式写入 `data/config.json` 的 `box_mode`，经 `GET/POST /api/config` 持久化。A 股 / 币圈扫描与 `/api/kline` 都读该字段。

| mode | 看板 | 状态 |
|---|---|---|
| `classic` | 经典 | **默认**，行为与本 fork 原 `compute_box` 一致 |
| `p0` | P0增强 | 本 PR 实现 |
| `p1` | P1通道(soon) | **骨架**：API 枚举 + 看板禁用；计算回退 classic 并标注 `p1_status=not_implemented` |

切换模式后：K 线叠加会按当前模式现算（`/api/kline` 用缓存 bars 重算 box）；评分卡片仍是上一次扫描的结果，需 **强制重扫**。模式与扫描结果 `box_mode` 不一致时，1 小时扫描缓存不会命中。

## classic（默认）

- 窗口：最近 60 根；若最近 15 根内收盘有效突破「前 40 根最高价 × 1.005」，窗口截止到突破日（不含突破 K）。
- 边界：`box_high = max(High)`，`box_low = min(Low)`。
- 试盘（需同时满足）：
  - `High >= box_high × 0.985`（`BOX_NEAR`）
  - `Close <= box_high × 1.005`（`BOX_CLOSE`）
  - `Volume >= 0.70 × 窗口均量`（`TEST_VOL`）
  - 上影占比 `>= 0.30`（`BOX_SHADOW`），**或** `High >= box_high × 0.995`

## p0（本 PR 定稿参数）

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

## p1（后续，Tom）

计划：上升通道 = OLS 拟合中轴 + 残差分位数通道带 + ADX 过滤「无趋势箱体」。当前 `compute_box_p1` 回退 classic，返回：

```json
{ "box_mode": "p1", "p1_status": "not_implemented", "box_quality": "p1_pending", "note": "..." }
```

看板选项禁用，避免误选后当成真实通道。

## 上沿事件：试盘 vs 真突破

相对**当前模式**算出的上沿 `R`（classic=窗口最高，p0=High 0.95 分位）给刺穿 K 分类。与各模式自己的 `tests` 计分**并行**，四条件 100 分制本 PR **不改**。

触发前提：`High >= R`（触及或刺穿）。

| `kind` | 含义 | 定稿规则 |
|---|---|---|
| `test` | 试盘 / 假突破 | `Close ≤ R×1.005`，上影/(H−L)≥**0.4**，上影≥**2×**实体，量≥**1.8×**MA20 |
| `breakout_candidate` | 潜在突破 | `Close ≥ R×1.015`（α=**1.5%**），实体/振幅≥**0.60**，上影/振幅≤**0.15**，量≥**1.8×**MA20 |
| `breakout_confirmed` | 确认真突破 | 先满足 candidate，且随后 **2** 根收盘仍 **> R**。**事后标签**，需要 t+2 已收盘；最新一根不会标 confirmed |
| `breakout_failed` | 失败突破 | candidate 但下一根收盘 **< R**（或 t+2 未能站上） |
| `none` | 未归类 | 未刺穿，或收盘落在 `R×1.005` 与 `R×1.015` 之间，或形态/量能不够 |

字段（箱体 / 扫描行 / `/api/kline` 的 `box`）：

- `edge_event`：最后一根 K 的分类
- `last_edge_event` / `last_edge_event_date`：窗口起最近一次非 none 事件
- `edge_events`：最近最多 12 条 `{date, kind, index}`
- `breakout_candidates`：candidate+confirmed+failed 根数
- `breakout_confirmed` / `breakout_failed`：计数
- `tests`：仍是该模式自己的试盘计数（评分用）

看板：最新一根为试盘/突破时卡片小徽章；K 线悬浮提示该根若在 `edge_events` 里会写出中文标签。

