# 本地滚动 K 线库与 4 小时后台同步

看板主路径改为**只读本地分析结果**。全市场标的（`market=crypto`）的 K 线由后台维护，不再在主页点「全市场扫描」现场拉 Gate。

## 范围

| 市场 | 是否进 180 根库 | 说明 |
|---|---|---|
| **全市场标的**（币 / 金 / 美股代币 / 日韩代理 / 指数） | 是 | 配置池 + 默认 Gate 宇宙。这是 VPS 变慢的主因。 |
| **A 股** | **否** | 全市场 5000+ 不写入滚动库。主页只读上次 `data/watchlist.json`。可选「立即扫描 A 股」埋在「标的/数据」配置里。 |

## 文件

| 路径 | 作用 |
|---|---|
| `kline_store.py` | SQLite 滚动库、merge/trim、拉取状态 |
| `sync_worker.py` | 增量同步 + 用库存 K 线跑 `analyze_crypto` |
| `data/kline_store.sqlite` | 运行期库（gitignore） |
| `data/maintained_pool.json` | 每票 `source` 覆盖与全局 `default_source` |
| `data/global_override_pool.json` | 非空 = 只维护这些代码；空 = 默认混合宇宙 |
| `data/crypto.json` | 分析落盘（看板 `GET /api/crypto` 仍读它） |

## Schema（`data/kline_store.sqlite`）

- `symbols(code PK, name, asset_class, source, source_override, origin, enabled, …)`
- `bars(symbol, interval, ts, date, open, high, low, close, vol)` 主键 `(symbol, interval, ts)`
- `pull_status(symbol, interval, last_start, last_end, last_success, last_error, bar_count, source, lag_sec)`
- `analysis_runs(id, started_at, ended_at, interval, n_symbols, n_ok, n_fail, status, error)`
- `job_state(key, value)`：`last_sync_*`、`ticker_snapshot`、`universe_snapshot` 等

**滚动上限：每个 `(symbol, interval)` 最多 180 根。** 合并时按 `date` 去重（后写覆盖），按时间排序后只留最新 180 根。

`interval` 与配置 `crypto_interval` 一致（`4h` / `8h` / `1d`）。后台同步**只维护当前配置周期**。

## 增量拉取

1. 该 `(symbol, interval)` 为空：回填最多 180 根。
2. 否则：按距 `last_ts` 的间隔估算根数 + 3 根重叠，向源请求，与库存合并去重，再裁到 180。
3. 源优先级与扫描时代相同（见 [crypto-global-pool.md](crypto-global-pool.md)），可被每票 / 全局 `source` 覆盖：`auto` / `gate` / `crypto` / `sina` / `naver`。

CI **不打 live Gate**；同步逻辑用 fixture bars 单测。

## 4 小时作业

- 配置 `kline_sync_hours`，默认 **4**（钳制 1–24）。
- `server.py` 守护线程 `kline_sync_loop`：listen 之后约 25 秒错开跑第一轮，之后到期再跑。不阻塞端口绑定。
- 每轮：增量拉 K 线 → `analyze_crypto` / 箱体 / 旗形 / 趋势线 → 写 `data/crypto.json`，带 `analysis_as_of`、`bars_as_of`、`from_store`。
- 与 A 股 `scheduler_loop`（交易日 11:30 / 15:00）共用 `STATE.scanning`，不会双开。

## API

| 接口 | 说明 |
|---|---|
| `GET /api/kline_store/status` | 总览 + 每票 last_pull / lag / error / bar_count / source |
| `GET/POST /api/kline_store/pool` | 列表；`add` / `delete` / `update` 源 / `clear` 恢复默认 |
| `POST /api/kline_store/sync` | 立即同步；`{symbols?, retry_failed?, skip_analyze?}` |
| `POST /api/kline_store/analyze` | 只用库存 K 线重算评分 |
| `GET /api/crypto` | 仍读 `crypto.json`（后台写入） |
| `GET /api/kline` | **先读本地库**；仅缺数据时一次性回填并写入库 |
| `POST /api/scan` `mode=crypto` | 仍可用，内部改为同步+分析；主 UX 已从顶栏移除 |
| `POST /api/scan` `mode=market` | 仅配置里的「立即扫描 A 股」 |

## 看板

顶栏「覆盖」改为 **「标的/数据」**：池表格、添加/删除、每行数据源、状态条、立即同步 K 线 / 立即分析 / 失败重试 / 立即扫描 A 股。

主页**没有**「全市场扫描 / 全市场标的扫描 / 强制重扫」。从未同步时空态指向配置面板。进度条仍在配置触发的任务运行时显示。

## A 股说明（务必）

不要把沪深全市场自动灌进 180 根库——体量与公开接口都不合适。A 股 Tab 只展示上次扫描写入的 `watchlist.json`。需要刷新时在配置面板点「立即扫描 A 股」（仍走原 `run_market_scan`，约 10–30 分钟）。
