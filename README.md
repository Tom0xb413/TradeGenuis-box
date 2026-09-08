# TradeGenuis · 箱体突破战法看板

一个基于**公开行情接口、全自动**的箱体突破选股/选币看板。把「箱体突破四条件」这套超短线战法做成可实时运行的扫描引擎：拉真实数据 → 计算机械条件 → 打分 → 达标标的直接以图形化卡片呈现。**A股与全市场标的双市场**，一套箱体引擎复用。

> 品牌：TradeGenuis（交易工具）。深色主题沿用 TradingGenius 定稿（深藏蓝 `#1C223A` / 暖沙文字 `#E5D4B6` / 涨薄荷绿 / 跌珊瑚）。本项目为个人研究工具，**不构成投资建议**。

## 产品特性

- **A股全市场扫描**：沪深 5000+ 只逐一深度计算，无粗筛（`--market`），也可量比粗筛快扫（`--quick`）
- **全市场标的扫描**：USDT 永续涨幅前 **20** + 黄金（Gate `XAUT_USDT`）+ 美/日/韩指数与龙头。有 Gate **股票代币** 的标的走原生 4h/8h/1d（跟踪正股、有基差，非交易所官方行情）；无代币的美股走新浪分钟K，日韩正股走 Sina/Naver 日K。覆盖池可点顶栏「覆盖」编辑。详见 [docs/crypto-global-pool.md](docs/crypto-global-pool.md)
- **四条件机械打分**（A股各 25 分，≥85 达标）：
  1. 热点题材 —— 当日涨幅前 3 概念板块 + 用户自定义关注板块
  2. 倍量启动 ≥3 日 —— 量 ≥ 前 5 日均量 1.8 倍连续计数
  3. 主力资金流入 + 高控盘 —— 近 5 日主力净流入 + 股东户数环比
  4. 箱体上沿试盘 ≥3 次 —— **可切换识别模式**（见下）
- **箱体识别多模式**（横幅「箱体模式」，写入 `data/config.json` 的 `box_mode`）：
  - **经典**（默认）：60 日窗口最高/最低 + 原试盘规则，行为与历史版本一致
  - **P0增强**：分位数边界（High 95% / Low 5%）+ 振幅门控 15% + 更严上影/量能试盘
  - **P1通道**：斜向通道（OLS 中轴 + 残差 95%/5% 分位带 + 温和斜率/R²/ADX）；K 线画斜轨
  - 切换后 K 线箱体立即按新模式绘制；评分需「强制重扫」。参数与字段见 [docs/box-modes.md](docs/box-modes.md)
  - **试盘 vs 真突破**（元数据，不改四条件满分）：相对当前模式上沿 R 标注 `edge_event`（`test` / `breakout_candidate` / `breakout_confirmed` / `breakout_failed`）。确认突破为 t+2 事后标签
- **形态族**（横幅「形态」，写入 `pattern_family`，默认 `box` 不打断现有用户）：
  - **箱体/通道**（`box`）：上述箱体模式与四条件评分
  - **高位旗形(杯柄)**（`high_flag`）：放量 pole → 高位浅回撤缩量旗面 → 可选二次买点。批量筛选「仅旗形 / 仅二次买点」，不改四条件 100 分。参数见 [docs/pattern-high-flag.md](docs/pattern-high-flag.md)
  - **趋势线**（`trendline`）：摆动高低点连成上升支撑 / 下降压力 L(t)；收盘越过即结构改变。筛选「全部有线 / 刚跌破支撑 / 刚突破压力」。参数见 [docs/pattern-trendline.md](docs/pattern-trendline.md)
  - 切换形态后须强制重扫；扫描缓存身份含 `pattern_family`，避免串用 1 小时结果
- **结果优先的图形化看板**：只展示达标标的，每张卡片内嵌 K 线（含成交量、箱体虚线、悬浮十字提示）、四条件状态、评分徽章
- **自动扫描调度**：每个交易日 11:30（午间收盘）/ 15:00（收盘）各扫一次，服务端常驻调度
- **共享扫描进度**：全市场 / 快扫 / 全市场标的 / 自选池扫描把结构化进度写入服务端 `scan_progress`；所有打开中的看板每秒轮询 `GET /api/status`，顶部显示同一条进度条与百分比。进页时若服务器已在扫，无需点击也会自动出现进度条；扫描进行中再点扫描会 **加入当前任务**（`status: running`），不会开第二轮
- **扫描并发**：默认 **16** 线程（`scan_workers`，钳制 4–32），自选池也已并行。上游限流报错增多时可把并发降到 8 或 4（见下）
- **实时行情刷新**：达标标的每 3 秒静默刷新价格/涨跌
- **K 线懒加载**：卡片进入视口附近才请求 `/api/kline` 并绘制（IntersectionObserver），同时最多 4 路并发，避免进页时按卡片数打满上游；全市场标的仍列出全部进池标的，优先加载视口内卡片，达标卡在队列中优先
- **日 K 服务端缓存**：日线内存 TTL 约 45 分钟，并落盘 `data/kline_cache/`（按市场+代码+日期）；空数据与错误响应不缓存。与「扫描结果 1 小时缓存」相互独立
- **Telegram 推送**（可选）：扫描结果推送至群/私聊
- **零数据库、零 API Key**：全部依赖公开接口（腾讯/新浪/东方财富/Binance/Gate.io，东财失败时 AKShare 降级），结果即 JSON 文件
- **本 fork 增强**（相对上游）：
  - 新浪全市场名单改为 `sh_a` + `sz_a` 分市场拉取（不用 `hs_a`），收盘后价格回退结算价——国内 VPS 上验证过
  - 全市场标的：Binance 主源，失败后**粘性**回退 Gate.io USDT 永续；黄金优先 `XAUT_USDT`；美/日/韩有代币走 Gate 股票代币（非官方正股），无代币美股走新浪分钟K，日韩走 Naver/Sina 日K（勿依赖 Yahoo）
  - 东财 push2 全挂时经 AKShare 走新浪概念/交易所官方名单/资金流等非 push2 接口，扫描尽量完成而非中止
  - 看板「全市场扫描 / 全市场标的扫描」默认使用 **1 小时结果缓存**；「强制重扫」或交易日 11:30/15:00 调度会绕过缓存
  - 扫描默认 16 线程（`scan_workers` / `SCAN_WORKERS`，钳制 4–32）；多浏览器共享同一条 `scan_progress` 进度条
  - 看板 K 线视口懒加载 + 4 路并发；日 K 45 分钟内存/磁盘缓存（`data/kline_cache/`）

## 快速启动

```bash
git clone <repo-url> && cd <repo>
bash start.sh
```

启动后打开 **http://127.0.0.1:8808**。`start.sh` 会自动装依赖（`requests`、`akshare`）、首次无数据时后台启动一次全市场扫描。

页面加载即展示 `data/watchlist.json` / `data/crypto.json` 缓存（若有）。一小时内再点「全市场扫描」会直接返回缓存；需要最新结果时点「强制重扫」。

手动启动（分步）：

```bash
pip install -r requirements.txt
python3 scanner.py --market     # ① 扫描（A股全市场，约 10–30 分钟）
python3 server.py               # ② 启动看板 → http://127.0.0.1:8808
```

## 双市场扫描

```bash
python3 scanner.py --market     # A股：沪深全市场逐一深度计算
python3 scanner.py --market --quick   # A股快扫：量比粗筛 TOP 200
python3 scanner.py --crypto     # 全市场标的：永续涨幅前 20 + 黄金 + 美/日/韩指数与龙头
python3 scanner.py              # 自选池（data/pool.json）
```

看板顶部横幅可一键切换 **A股 / 全市场标的** 两个 tab；扫描按钮随 tab 自动切换目标市场。全市场标的 Tab 为全球混合池（币 TOP20 + 黄金 + 美/日/韩）。币走 Binance，超时粘性 Gate；股票有 Gate 代币则用代币 K 线（4h/8h/1d），无代币美股用新浪分钟K，日韩正股用日K。顶栏「覆盖」可配置覆盖池。周期胶囊 **4h / 8h / 1日** 写入 `crypto_interval`。

### 扫描并发 `scan_workers`

自选池、全市场、全市场标的扫描共用同一套线程池大小，默认 **16**，允许范围 **4–32**。

优先级：命令行 `--workers` > 环境变量 `SCAN_WORKERS` > `data/config.json` 的 `scan_workers`。

```bash
export SCAN_WORKERS=8
python3 scanner.py --market
# 或
python3 scanner.py --market --workers 8
```

公开行情接口有速率限制。若日志里 HTTP 502/限流/空 K 线明显增多，把并发降到 8 或 4。看板多浏览器看到的是**同一份**服务端进度，不是各点各扫。

## 自定义关注板块

看板横幅「关注板块」输入框添加，存于 `data/sectors.json`。系统会将你填写的板块名**自动匹配到东方财富概念板块分类**，纳入扫描范围（与当日涨幅前 3 板块并列）。

## Telegram 推送（可选）

1. `@BotFather` 建 Bot 拿 Token；给 Bot 发条消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 查 `chat.id`
2. 配置：

```bash
export TG_BOT_TOKEN=123456:ABC
export TG_CHAT_ID=123456789
python3 scanner.py --test-push   # 连通测试
python3 scanner.py --push        # 扫描并推送
```

也可在看板 banner 里直接填 Token/Chat ID（保存到 `data/config.json`，`--push` 会自动读取）。

## 文件结构

| 文件 | 说明 |
|---|---|
| `start.sh` | 一键启动脚本（装依赖 + 首次扫描 + 起服务） |
| `scanner.py` | 扫描引擎：拉数据 → 四条件打分 → 写 JSON / 推 Telegram |
| `box_engine.py` | 箱体识别：`classic` / `p0` / `p1` 斜向通道 |
| `pattern_flag.py` | 高位旗形 / 杯柄（柄）：pole + 缩量旗面 + 二次买点 |
| `pattern_trendline.py` | 趋势线：摆动点连线 + Close 越线事件 |
| `server.py` | 本地看板服务器（纯标准库，默认端口 8808） |
| `dashboard.html` | 看板页（TradeGenuis 深色主题，自托管字体） |
| `docs/box-modes.md` | 箱体模式参数（含 P1 定稿） |
| `docs/pattern-high-flag.md` | 高位旗形参数与 Bull Flag / 杯柄 / 突破中继 |
| `docs/pattern-trendline.md` | 趋势线参数与 Tom 图例（价格×时间边界） |
| `global_pool.py` | 全球池常量、Gate 股票代币映射、覆盖池 |
| `equity_sources.py` | 无代币时的美股分钟K/日K、日韩日K（Sina / Naver） |
| `docs/crypto-global-pool.md` | 全球池宇宙、Gate 代币、覆盖池、周期规则 |
| `data/pool.json` | 自选池（`code` 必填） |
| `data/sectors.json` | 用户自定义关注板块 |
| `data/*.json` | 扫描结果与缓存（自动生成，已在 .gitignore） |
| `data/kline_cache/` | 日 K 磁盘缓存（按市场+代码+日期，自动生成） |
| `static/fonts/` | 自托管字体（Outfit / IBM Plex Mono） |

## 说明与风险

- 行情/资金/户数来自公开接口（腾讯、新浪、东方财富、Binance、Gate.io、Naver），有延迟，盘中为实时快照；东财 push2 在部分国内 VPS 上会断开，脚本含新浪分市场、腾讯 K 线、AKShare（非 push2）与 Gate.io 多源兜底。美股/指数优先 Gate **股票代币**（与正股有基差），无代币再新浪/Naver；勿依赖 Yahoo（VPS 上常见 403）
- 部分 AKShare 接口（资金流/股东户数）底层仍可能访问东财数据中心（非 push2）；失败时该条件按「无数据」弱化打分，不中止整轮扫描
- 箱体、倍量、试盘均为机械规则近似；股东户数为季度披露，是筹码集中度的**代理指标**且滞后
- 超短线假突破风险高，请自行控制仓位与止损。历史表现不代表未来收益，本项目不构成投资建议
