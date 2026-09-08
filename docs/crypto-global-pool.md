# 全市场标的 Tab：全球池、多源行情与覆盖池

全市场标的页（内部仍为 `data-market="crypto"` / `/api/crypto`，以免打断缓存路径）扫描一个**混合全球池**。A 股 Tab、四条件打分、形态族算法不变；全球池复用同一套 OHLC 形态引擎，评分走原币圈 3 条件口径。

常量集中在 [`global_pool.py`](../global_pool.py)（含 `GATE_EQUITY_MAP`），无代币时的日K / 美股分钟K 在 [`equity_sources.py`](../equity_sources.py)。

## 股票代币 / 代币代理（重要）

美/日/韩 **有 Gate 市场** 的标的走 **代币化加密市场**（原生 **4h / 8h / 1d**，VPS 可直连）。**不是**纽交所 / 东证 / 韩交所官方打印，跟踪正股但存在基差。

优先级（VPS 表）：

1. **USDT 永续干净名**（`AAPL_USDT`、`SPX500_USDT`、`SONY_USDT`）
2. 失败再试现货 `*X` / `*G` / `*ON`（如 `AAPLX_USDT`）。**不用** `3L`/`3S`
3. 仍无：美股/美指新浪 `getMinK`；日韩正股日K

日韩 Gate 标的（索尼、三星）看板标 **代币代理**；美股/美指标 **股票代币**。

| 内部代码 | 主路径（永续） | 现货回退 | 说明 |
|---|---|---|---|
| US20 除 MA | `{TICKER}_USDT`，伯克希尔 `BRKB_USDT` | 有核对过的 `*X`/`*G` | `WMTX` 不是沃尔玛 |
| `MA` | （无） | `MAX_USDT` | 永续无 MA；现货 `MA_USDT` 是 Mind AI |
| `.INX` | `SPX500_USDT` | — | **不要** `SPX_USDT`（梗币） |
| `.DJI` | `US30_USDT` | — | **不要** `DIA_USDT`（加密 DIA） |
| `.NDX` | `NAS100_USDT` | — | |
| `.IXIC` | （无） | | 新浪分钟K |
| `SPY`/`QQQ`/`IWM`/`SQQQ` | 对应 `_USDT` 永续 | `SPYX`/`QQQX` | 覆盖池 ETF |
| `7203.T` | （无） | | 日元丰田日K。无 TOYOTA 代币 |
| `6758.T` | `SONY_USDT` | | **代币代理** |
| `005930`/`000660` | `SAMSUNG_USDT` / `SKHYNIX_USDT` | | **代币代理** |
| `NKY`/KOSPI/KOSDAQ | （无） | | 日K。勿用 `JPN225_USDT` |

覆盖池额外永续：`COIN` `BABA` `AMD` `ARM` `PLTR` `HOOD` `MSTR` `IBM` `ORCL` `TM`。`AAPL` 与 `AAPL_USDT` 都解析到 Gate。

无对应代币时：

- **美股 / 美指 / 美股 ADR**：新浪 `US_MinKService.getMinK`（`type=240` 约 4h；`type=60` 约 1h 再合成 8h）→ 再不行才日K
- **日韩正股 / 日经 / KOSPI**：Sina / Naver **日K**，4h/8h 带 `interval_note`

## 宇宙（默认混合池）

| 类别 | `asset_class` | 数量 | 主源 |
|---|---|---|---|
| USDT 永续涨幅榜 | `crypto` | `CRYPTO_TOP_N = 20` | Binance `fapi` 24h ticker，失败后**粘性** Gate.io；股票代币 / xStock / 杠杆合约不占 TOP20 |
| 黄金（1 只） | `gold` | 1 | Gate **`XAUT_USDT` 现货**（及永续；不要用 `XAU_USDT` 当首选）→ `XAUUSDT` / `PAXGUSDT` |
| 美股指数 | `us_index` | 4 | Gate 代币（标普/道指/纳指100）或新浪分钟K/日K（纳指综合） |
| 美股大盘 | `us_stock` | `US_STOCKS` 约 20 | 永续干净名；失败再现货 *X；再无则新浪 getMinK |
| 日经 225 | `jp_index` | 1 | 新浪 `gi.finance.sina.com.cn/hq/daily?symbol=NKY` |
| 日股龙头 | `jp_stock` | 2 | 索尼走 Gate `SONY_USDT`；丰田 `7203.T` 走 Naver 日K |
| KOSPI / KOSDAQ | `kr_index` | 2 | Naver `siseJson.nhn` |
| 韩股龙头 | `kr_stock` | 2 | Gate `SAMSUNG_USDT` / `SKHYNIX_USDT`，失败再 Naver |

合计约 **20 + 1 + 4 + 20 + 1 + 2 + 2 + 2**。黄金永续若出现在涨幅榜里，**不占用** TOP20 名额。

看板 `market` 仍为 `crypto`。卡片徽章区分 币 / 金 / 美指(代币) / 美股(代币) / 日股(代币) / 韩股(代币)。

## 明确不用的源

- **Yahoo Finance**（Tom 的 VPS 上 HTTP 403）。扫描与 K 线主路径不再 Yahoo-first。
- Stooq
- 东财 **push2his K 线**（push2delay **报价**仅作无代币时的校验备份）
- Finnhub / Alpha Vantage（需要 Key）
- 杠杆/反向代币（`3L`/`3S`、CSOP 2L 等不当默认；`*X` 仅在确认为 xStock 时使用）
- Twelve Data demo（仅 AAPL/QQQ，不作通用源）

## 黄金符号

优先级（成功即停，失败则降级，整轮扫描不中止）：

1. **Gate `XAUT_USDT` 现货**（及永续 ticker / K 线）
2. 交易所黄金永续 `XAUUSDT` / `PAXGUSDT`

展示名固定为「黄金」。`crypto.json` 的 `gold` 字段记录实际 `code` 与 `source`。

## K 线周期 `crypto_interval`

写入 `data/config.json`，经 `GET/POST /api/config` 暴露。非法值规范化为 `1d`。

| 值 | 币 / 金 / **股票代币**（Gate 原生） | 无代币美股 / 美指 | 日韩正股 / 日经 / KOSPI |
|---|---|---|---|
| `4h` / `8h` | 原生周期 | 新浪 `getMinK` | **无稳定多日分钟历史** → 回退 **日K**，并带 `interval_note` |
| `1d` | 日K | 日K | 日K（新浪 / Naver） |

股票代币 K 线**不**走 Binance。主路径永续干净名，失败再现货 `*X`/`*G`。未上市才回退新浪。日韩真·1h 需 VPN/Yahoo。

## 覆盖池

顶栏 **「覆盖」** 可点开编辑器：textarea 填符号（逗号或换行），保存前逐只 `validate_symbol`。

- 持久化：`data/global_override_pool.json`（`{"symbols":[...]}`）
- **非空**：扫描**只扫这些标的**（仍应用周期与形态）
- **空**：默认混合池
- 校验：`AAPL` / `AAPL_USDT` / `AAPLX` / `PLTR` / `SAMSUNG` → Gate 市场 / 黄金 / Sina / Naver
- 无效代码不写入；接口返回 `rejected: [{code, reason}]`
- 药丸显示：覆盖时「N 只」，否则「默认」

```
POST /api/global_pool/validate  {symbols:[]} → {ok:[], bad:[{code,reason}]}
GET  /api/global_pool/override
POST /api/global_pool/override  {symbols:[]} | {action:"clear"}
```

扫描 1 小时缓存身份含 `override_fingerprint`，改覆盖池后不会误用旧结果。

## 缓存身份

扫描结果对全市场标的 Tab 额外比对 `crypto_interval` 与覆盖池指纹。K 线缓存键在 `market=crypto` 时含周期。

## 失败降级

- 币所 24h ticker 全失败：仍扫描黄金（若可得）+ 指数 + 股票，或只扫覆盖池
- Gate 代币 K 线失败：美股回退新浪分钟K/日K；日韩回退 Naver/Sina 日K
- 单票超时或不足 40 根：跳过该票，进度结束时汇报 `skipped`
- 不引入 `yfinance`

## 看板

- Tab 文案 **全市场标的**（内部 `data-market="crypto"` 不变）
- 周期胶囊 **4h / 8h / 1日**
- 池筛选：全部 / 仅币 / 仅美股 / 日股 / 韩股 / 黄金+指数
- 有代币时徽章带「代币」，副标题展示 Gate 合约名
- 「覆盖」编辑覆盖池
