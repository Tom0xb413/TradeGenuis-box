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
