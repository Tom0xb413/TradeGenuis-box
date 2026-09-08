# 全市场标的 Tab：全球池、多源行情与覆盖池

全市场标的页（内部仍为 `data-market="crypto"` / `/api/crypto`，以免打断缓存路径）扫描一个**混合全球池**。A 股 Tab、四条件打分、形态族算法不变；全球池复用同一套 OHLC 形态引擎，评分走原币圈 3 条件口径。

常量集中在 [`global_pool.py`](../global_pool.py)，行情适配在 [`equity_sources.py`](../equity_sources.py)。改列表即可改默认池。

## 宇宙（默认混合池）

| 类别 | `asset_class` | 数量 | 主源（VPS 实测） |
|---|---|---|---|
| USDT 永续涨幅榜 | `crypto` | `CRYPTO_TOP_N = 20` | Binance `fapi` 24h ticker，失败后**粘性** Gate.io |
| 黄金（1 只） | `gold` | 1 | Gate **`XAUT_USDT`**（不要用 `XAU_USDT` 当首选）→ `XAUUSDT` / `PAXGUSDT` |
| 美股指数 | `us_index` | 4 | 新浪 JSONP：`.INX` `.DJI` `.IXIC` `.NDX` |
| 美股大盘 | `us_stock` | `US_STOCKS` 约 20 | 新浪 `US_MinKService.getDailyK` + `hq.sinajs.cn/list=gb_{sym}`；备份 Naver `{SYM}.O/.N` |
| 日经 225 | `jp_index` | 1 | 新浪 `gi.finance.sina.com.cn/hq/daily?symbol=NKY`；备份 `znb_NKY` 报价 |
| 日股龙头 | `jp_stock` | 2（丰田 `7203.T` / 索尼 `6758.T`） | Naver `api.stock.naver.com/stock/{code}.T/price` 分页；备份东财 push2delay **报价** `176.{code}` |
| KOSPI / KOSDAQ | `kr_index` | 2 | Naver `fchart.stock.naver.com/siseJson.nhn`；KOSPI 备份 `ak.index_global_hist_sina("首尔综合指数")` |
| 韩股龙头 | `kr_stock` | 2（三星 `005930` / SK 海力士 `000660`） | 同上 Naver siseJson |

合计约 **20 + 1 + 4 + 20 + 1 + 2 + 2 + 2**。黄金永续若出现在涨幅榜里，**不占用** TOP20 名额。

看板 `market` 仍为 `crypto`。卡片徽章区分 币 / 金 / 美指 / 美股 / 日股 / 韩股。

## 明确不用的源

- **Yahoo Finance**（Tom 的 VPS 上 HTTP 403）。本机若走 VPN，Yahoo 1h 仍可能可用，代码里保留 `fetch_yahoo_instrument` 遗留函数，但**扫描与 K 线主路径不再 Yahoo-first**。
- Stooq
- 东财 **push2his K 线**（push2delay **报价**仅作日股/美股校验备份）
- Finnhub / Alpha Vantage（需要 Key）

## 黄金符号

优先级（成功即停，失败则降级，整轮扫描不中止）：

1. **Gate `XAUT_USDT`**（内部代码 `XAUTUSDT`，Tether Gold）
2. 交易所黄金永续 `XAUUSDT` / `PAXGUSDT`（Gate 上 `XAU_USDT` 仍存在，但流动性/品种与 XAUT 不同，只作次选）

展示名固定为「黄金」。`crypto.json` 的 `gold` 字段记录实际 `code` 与 `source`。

## K 线周期 `crypto_interval`

写入 `data/config.json`，经 `GET/POST /api/config` 暴露。非法值规范化为 `1d`。

| 值 | 币 / 金（Gate 原生） | 股票 / 指数 |
|---|---|---|
| `4h` / `8h` | 原生周期 | **无稳定多日分钟历史** → 回退 **日K**，并带 `interval_note`（UI 提示） |
| `1d` | 日K | 日K（新浪 / Naver） |

诚实回退优于空图。4h/8h 的 `date` 对币仍为 `YYYY-MM-DD HH:MM`；股票日K 为 `YYYY-MM-DD`。

## 覆盖池

顶栏 **「覆盖」** 可点开编辑器：textarea 填符号（逗号或换行），保存前逐只 `validate_symbol`。

- 持久化：`data/global_override_pool.json`（`{"symbols":[...]}`）
- **非空**：扫描**只扫这些标的**（仍应用周期与形态）
- **空**：默认混合池
- 校验源：永续 ticker 列表 / Gate 黄金 / 新浪美股 / Naver 日股 `.T` / Naver 韩股 / 已知指数别名
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
- 单票超时或不足 40 根：跳过该票，进度结束时汇报 `skipped`
- 不引入 `yfinance`

## 看板

- Tab 文案 **全市场标的**（内部 `data-market="crypto"` 不变）
- 周期胶囊 **4h / 8h / 1日**
- 池筛选：全部 / 仅币 / 仅美股 / 日股 / 韩股 / 黄金+指数
- 标题「全市场标的」及各类数量
- 「覆盖」编辑覆盖池
