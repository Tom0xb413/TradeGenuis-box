# 加密货币 Tab：全球池与 K 线周期

加密货币页不再只扫「24h 涨幅 TOP30 USDT 永续」，而是一个**混合全球池**。A 股 Tab、四条件打分、形态族（箱体 / 高位旗形 / 趋势线）算法不变；全球池标的复用同一套 OHLC 形态引擎，评分走原币圈 3 条件口径。

常量集中在 [`global_pool.py`](../global_pool.py)，改列表即可改池子，不必改扫描主流程。

## 宇宙

| 类别 | `asset_class` | 数量 | 来源 |
|---|---|---|---|
| USDT 永续涨幅榜 | `crypto` | `CRYPTO_TOP_N = 20`（原 30） | Binance `fapi` 24h ticker，失败后**粘性** Gate.io USDT 永续 |
| 黄金（1 只） | `gold` | 1 | 见下 |
| 美股指数 | `us_index` | `US_INDICES`（默认 4） | Yahoo chart：`^GSPC` 标普500、`^DJI` 道指、`^IXIC` 纳指、`^NDX` 纳斯达克100 |
| 美股大盘 | `us_stock` | `US_STOCKS` 约 20 | Yahoo：AAPL / MSFT / NVDA / … / NFLX |

合计约 **20 + 1 + 4 + 20**。黄金永续若出现在涨幅榜里，**不占用** TOP20 名额。

看板 `market` 仍为 `crypto`（同一 Tab、`/api/crypto`、`/api/kline?market=crypto`）。卡片用徽章区分 币 / 金 / 指数 / 美股。

## 黄金符号（运行时选一只）

优先级（成功即停，失败则降级，整轮扫描不中止）：

1. **Binance / Gate 黄金永续**（与现有币圈后端相同，含粘性 Gate）：`XAUUSDT` → `PAXGUSDT`
2. **Yahoo**：`GC=F`（COMEX 黄金期货）→ `GLD`（SPDR 黄金 ETF）

本仓库验证环境（Binance 官方超时后粘性 Gate）：**最终选用 `XAUUSDT`（Gate USDT 永续）**，24h 成交额充足，展示名「黄金」。`PAXGUSDT` 同样存在但流动性更低，仅作次选。Yahoo `GC=F`/`GLD` 在部分 IP 上会 HTTP 429，代码会短暂重试后跳过。

展示名固定为「黄金」。扫描结果 `crypto.json` 的 `gold` 字段记录实际 `code` 与 `source`（`crypto` 或 `yahoo`）。国内 VPS 上若币所与 Yahoo 都超时，该行被跳过并计入 `skipped`。

## K 线周期 `crypto_interval`

写入 `data/config.json`，与 `box_mode` / `pattern_family` 一样经 `GET/POST /api/config` 暴露。

| 值 | 含义 | 默认 |
|---|---|---|
| `4h` | 4 小时 K | |
| `8h` | 8 小时 K | |
| `1d` | 日 K | **是** |

非法值规范化为 `1d`。`GET /api/config` 另给 `crypto_intervals: ["4h","8h","1d"]` 供 UI 画胶囊。

- **Binance USDT 永续**：原生 `4h` / `8h` / `1d`
- **Gate.io USDT 永续**：同样使用 `4h` / `8h` / `1d` 字符串（已核对 [Futures candlesticks](https://www.gate.com/docs/developers/apiv4/en/#get-futures-candlesticks)）
- **Yahoo 股票 / 指数 / 黄金期货**：日线用 `interval=1d`；**4h/8h 没有稳定原生周期**，改为拉 `1h` 再按时钟整点重采样（`resample_ohlc_hours`）。美股 1h 仅交易时段，合成根数会少于 7×24 加密货币。

4h/8h 的 `date` 为 `YYYY-MM-DD HH:MM`，避免图上多根 K 叠成同一天。A 股日线仍为 `YYYY-MM-DD`。

## 缓存身份

扫描结果 1 小时缓存对加密货币 Tab 额外比对 `crypto_interval`（逻辑同 `box_mode` / `pattern_family` 不一致则不命中）。缺字段的旧 `crypto.json` 视为 `1d`。

K 线内存/磁盘缓存键在 `market=crypto` 时含周期，避免 4h 与 1d 串盘。

## 失败降级

- 币所 24h ticker 全失败：仍扫描黄金（若可得）+ 指数 + 美股
- 单票 Yahoo / K 线超时或不足 40 根：跳过该票，进度结束时汇报 `skipped`
- 不引入 `yfinance`，只用 Yahoo v8 chart HTTP（`query1` → `query2`，超时 10s）

## 看板

加密货币 Tab 显示：

- 周期胶囊 **4h / 8h / 1日**（切换会 POST config，并提示强制重扫）
- 池筛选：全部 / 仅币 / 仅美股 / 黄金+指数
- 标题「全球池」及各类数量
