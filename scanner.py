#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
箱体突破战法扫描器（全自动 · 接入真实行情数据）

四条件（各 25 分，≥85 达标推送）：
  1. 热点题材        —— 东财概念板块榜（5日+当日涨幅 TOP12）实时判定该股是否隶属热点板块
  2. 倍量启动持续≥3日 —— 腾讯前复权日K：成交量 ≥ 前 5 日均量 1.8 倍的连续天数
  3. 主力资金持续流入+高度控盘 —— 东财资金流（近5日主力净流入）+ 股东户数环比（筹码集中度）
  4. 箱体上沿试盘≥3次 —— 日K自动识别箱体（classic / p0 / p1 斜向通道，见 box_engine.py）
形态族 pattern_family：box（默认，四条件箱体）| high_flag（高位旗形/杯柄，见 pattern_flag.py）| trendline（趋势线，见 pattern_trendline.py）

数据源（全部公开接口，无需 Key；东财 push2 不可用时自动降级）：
  日K/现价 ：web.ifzq.gtimg.cn（腾讯）  备用 hq.sinajs.cn / money.finance.sina.com.cn
  资金流   ：push2his.eastmoney.com daykline  备用新浪资金流  再备用 AKShare
  股东户数 ：datacenter-web.eastmoney.com  备用 AKShare
  热点概念 ：push2.eastmoney.com clist + emweb F10  备用 AKShare（新浪概念 / 同花顺）
  全市场名单：东财 clist  备用新浪 sh_a+sz_a（不用 hs_a）  再备用 AKShare 官方名单
  币圈 Tab ：USDT 永续 TOP20 + 黄金 + 美股指数 + 固定美股（见 global_pool.py）
             Binance USDT 永续失败后粘性回退 Gate.io；美股/指数/部分黄金走 Yahoo chart
             K 线周期 crypto_interval：4h | 8h | 1d（默认 1d）

用法：
  pip install requests
  python3 scanner.py                  # 扫描 data/pool.json → 写 data/watchlist.json
  python3 scanner.py --push           # 扫描并推送 Telegram（需 TG_BOT_TOKEN/TG_CHAT_ID）
  python3 scanner.py --test-push      # Telegram 连通性测试
  python3 scanner.py --no-network     # 只用本地 watchlist 重算评分（离线）
  python3 scanner.py --push --cron    # cron 专用：静默，仅错误输出到 stderr
  python3 server.py                   # 启动本地看板页 http://127.0.0.1:8808
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from box_engine import (
    BOX_MODES,
    DEFAULT_BOX_MODE,
    box_row_fields,
    compute_box,
    compute_box_classic,
    compute_box_p0,
    compute_box_p1,
    normalize_box_mode,
)
from pattern_flag import (
    DEFAULT_PATTERN_FAMILY,
    PATTERN_FAMILIES,
    detect_high_flag,
    flag_row_fields,
    normalize_pattern_family,
)
from pattern_trendline import (
    detect_trendline,
    trendline_row_fields,
)
from global_pool import (
    CRYPTO_INTERVALS,
    CRYPTO_TOP_N,
    DEFAULT_CRYPTO_INTERVAL,
    GOLD_CRYPTO_SYMBOLS,
    GOLD_DISPLAY_NAME,
    GOLD_YAHOO_SYMBOLS,
    build_global_pool,
    fetch_yahoo_instrument,
    format_bar_date,
    is_yahoo_symbol,
    normalize_crypto_interval,
)

# --------------------------------------------------------------------------- #
# 常量 / 配置
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POOL_FILE = DATA / "pool.json"          # 扫描池（手工维护，唯一需要你编辑的文件）
WATCH_FILE = DATA / "watchlist.json"    # 扫描结果（自动生成）
BJT = timezone(timedelta(hours=8))

HOT_TOP_N = 10          # 热点概念板块取当日涨幅前 N 名（同时用于打分与板块筛选）
# CRYPTO_TOP_N 定义在 global_pool.py（加密货币 Tab 涨幅榜取前 N，当前 20）
CRYPTO_LOOKBACK = 200    # 币圈/全球池 K 线根数（随 crypto_interval 截取）
VOL_MULT = 1.8          # 倍量阈值（对前5日均量）
VOL_DAYS_REQ = 3        # 连续放量最少天数
FUND_DAYS = 5           # 资金流观察天数
FUND_INFLOW_REQ = 3     # 近5日主力净流入≥3天
HOLDER_HIGH = -2.0      # 股东户数环比 ≤ -2% → 高控盘
HOLDER_MID = 0.5        # ≤ 0.5% → 中控盘，否则偏低
TURNOVER_CAP = 15.0     # 换手率超 15% 视为分歧大，控盘降级
SCAN_CACHE_TTL = 3600   # 看板「全市场/币圈扫描」结果缓存秒数；调度任务强制刷新
SCAN_WORKERS_DEFAULT = 16
SCAN_WORKERS_MIN = 4
SCAN_WORKERS_MAX = 32
# 兼容旧名：实际并发以 resolve_scan_workers() 为准（环境变量 / config / 默认 16）
MARKET_WORKERS = SCAN_WORKERS_DEFAULT

# --------------------------------------------------------------------------- #
# HTTP 会话（自动重试）
# --------------------------------------------------------------------------- #
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
}
HTTP = requests.Session()
HTTP.headers.update(UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.4, status_forcelist=[502, 503, 504])),
)


def http_json(url: str, timeout: float = 10.0):
    r = HTTP.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} -> {url[:90]}")
    return r.json()


# --------------------------------------------------------------------------- #
# AKShare 可选降级（东财 push2 全挂时仍尽量完成扫描；缺包则静默跳过）
# --------------------------------------------------------------------------- #
# 实际调用与底层站点（刻意避开仍走 push2.eastmoney.com 的接口）：
#   stock_info_a_code_name        → 上交所/深交所官方 A 股名单（非东财）
#   stock_sector_spot("概念")     → 新浪概念板块涨跌幅（money.finance.sina.com.cn）
#   stock_sector_detail           → 新浪概念板块成分股
#   stock_fund_flow_concept      → 同花顺概念资金流榜（data.10jqka.com.cn，含涨跌幅）
#   stock_board_concept_cons_ths  → 同花顺概念成分股（q.10jqka.com.cn）
#   stock_individual_fund_flow   → 东财数据中心个股资金流（data.eastmoney.com，非 push2）
#   stock_zh_a_gdhs / gdhs_detail_em → 东财 datacenter 股东户数（非 push2）
# 不用：stock_zh_a_spot_em / stock_board_concept_name_em（底层仍是 push2 clist）
_hot_members: dict[str, list[str]] = {}   # 代码 → 本轮热点板块名（F10 失败时反查）
_ak_holder_map: dict | None = None        # 全市场股东户数快照（进程内一次）


def _ak_call(func_name: str, *args, **kwargs):
    """调用 AKShare 函数；未安装、接口仍打到东财失败、或签名变化时返回 None。"""
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return None
    fn = getattr(ak, func_name, None)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _df_records(df) -> list[dict]:
    if df is None:
        return []
    try:
        if getattr(df, "empty", True):
            return []
        return df.to_dict(orient="records")
    except Exception:
        return []


def _pick(row: dict, *keys):
    for k in keys:
        if k in row and row[k] is not None:
            v = row[k]
            try:
                if v != v:  # NaN
                    continue
            except Exception:
                pass
            if v == "":
                continue
            return v
    return None


def _to_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        s = str(v).strip().replace("%", "").replace(",", "")
        if not s:
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def now_str() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")


def iso_now() -> str:
    """北京时间 ISO-8601（带 +08:00），写入扫描结果 updated 字段。"""
    return datetime.now(BJT).isoformat(timespec="seconds")


def clamp_scan_workers(n) -> int:
    """把并发数钳制到 4–32；无法解析时回退默认 16。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return SCAN_WORKERS_DEFAULT
    return max(SCAN_WORKERS_MIN, min(SCAN_WORKERS_MAX, v))


def load_configured_scan_workers() -> int | None:
    """读取 data/config.json 的 scan_workers；缺失或非法则 None。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        v = cfg.get("scan_workers")
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def resolve_scan_workers(explicit: int | None = None) -> int:
    """
    扫描线程数：显式参数 > 环境变量 SCAN_WORKERS > config.json scan_workers > 16。
    结果钳制到 4–32。上游限流报错增多时可把并发降到 8 或 4。
    """
    if explicit is not None:
        return clamp_scan_workers(explicit)
    env = os.environ.get("SCAN_WORKERS") or os.environ.get("scan_workers")
    if env is not None and str(env).strip() != "":
        try:
            return clamp_scan_workers(int(str(env).strip()))
        except (TypeError, ValueError):
            pass
    cfg_n = load_configured_scan_workers()
    if cfg_n is not None:
        return clamp_scan_workers(cfg_n)
    return SCAN_WORKERS_DEFAULT


def emit_progress(progress, msg: str, **kwargs) -> None:
    """调用进度回调。兼容只接收 str 的旧 lambda，以及接受 phase/done/total 的新回调。"""
    if not progress:
        return
    try:
        progress(msg, **kwargs)
    except TypeError:
        progress(msg)


def parse_updated(payload: dict | None) -> datetime | None:
    """解析扫描结果时间戳：优先 updated（ISO），否则 as_of（北京时间墙钟）。"""
    if not payload:
        return None
    raw = payload.get("updated") or payload.get("as_of")
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1] + "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BJT)
        return dt
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
    except ValueError:
        return None


def cache_age_sec(payload: dict | None) -> float | None:
    dt = parse_updated(payload)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def is_fresh_scan_cache(payload: dict | None, ttl: int = SCAN_CACHE_TTL) -> bool:
    """完整扫描结果且年龄 < ttl 秒则视为可复用缓存（进行中的断点文件不算）。"""
    if not payload:
        return False
    if payload.get("done") is False:
        return False
    age = cache_age_sec(payload)
    return age is not None and age < ttl


def decorate_scan_payload(payload: dict) -> dict:
    """保证扫描结果含 ISO updated + items（与 candidates 同内容，兼容看板）。"""
    payload["as_of"] = payload.get("as_of") or now_str()
    payload["updated"] = iso_now()
    rows = payload.get("candidates")
    if rows is None:
        rows = payload.get("items") or []
        payload["candidates"] = rows
    payload["items"] = list(rows)
    if "box_mode" not in payload:
        payload["box_mode"] = load_box_mode()
    if "pattern_family" not in payload:
        payload["pattern_family"] = load_pattern_family()
    if payload.get("scope") == "crypto" and "crypto_interval" not in payload:
        payload["crypto_interval"] = load_crypto_interval()
    return payload


def load_box_mode() -> str:
    """读取 data/config.json 的 box_mode；缺省 classic。扫描路径据此选择识别算法。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        return normalize_box_mode(cfg.get("box_mode"))
    except Exception:
        return DEFAULT_BOX_MODE


def load_pattern_family() -> str:
    """读取 data/config.json 的 pattern_family；缺省 box，不改变现有用户行为。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        return normalize_pattern_family(cfg.get("pattern_family"))
    except Exception:
        return DEFAULT_PATTERN_FAMILY


def load_crypto_interval() -> str:
    """读取 data/config.json 的 crypto_interval；缺省 1d。仅加密货币 Tab 扫描/K线使用。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        return normalize_crypto_interval(cfg.get("crypto_interval"))
    except Exception:
        return DEFAULT_CRYPTO_INTERVAL


def is_trading_time() -> bool:
    """交易日 09:15-15:05 视为盘中。"""
    now = datetime.now(BJT)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


# --------------------------------------------------------------------------- #
# 数据获取
# --------------------------------------------------------------------------- #
def tx_symbol(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


def secid(code: str) -> str:
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    if code.startswith(("4", "8", "9")):
        return f"0.{code}"  # 北交所（含920xxx）东财口径
    return f"0.{code}"


def fetch_quote(code: str) -> dict:
    """实时行情：东财 push2 主源，腾讯 qt 兜底，新浪 hq 再兜底（不用 AKShare spot_em，其底层仍是 push2）。"""
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get"
            f"?secid={secid(code)}&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f170"
        )
        d = (http_json(url) or {}).get("data") or {}
        if d.get("f43"):
            return {
                "price": (d.get("f43") or 0) / 100,
                "chg": (d.get("f170") or 0) / 100,
                "name": d.get("f58") or "",
                "turnover": (d.get("f168") or 0) / 100,   # 换手率 %
                "volume_ratio": (d.get("f50") or 0) / 100,  # 量比
            }
    except Exception:
        pass
    # 腾讯兜底（GBK 文本，~ 分隔）
    try:
        r = HTTP.get(f"https://qt.gtimg.cn/q={tx_symbol(code)}", timeout=8)
        p = r.content.decode("gbk", errors="ignore").split("~")
        if len(p) >= 40 and p[3]:
            return {
                "price": float(p[3]),
                "chg": float(p[32]),
                "name": p[1],
                "turnover": float(p[38]),
                "volume_ratio": float(p[49]),
            }
    except Exception:
        pass
    q = _quote_sina(code)
    if q:
        return q
    raise RuntimeError("quote empty (em/tencent/sina)")


def _quote_sina(code: str) -> dict | None:
    """新浪 hq.sinajs.cn 单票快照（非 push2）。"""
    try:
        r = HTTP.get(
            f"https://hq.sinajs.cn/list={tx_symbol(code)}",
            timeout=8,
            headers={**UA, "Referer": "https://finance.sina.com.cn"},
        )
        text = r.content.decode("gbk", errors="ignore")
        if "=" not in text or '"' not in text:
            return None
        inner = text.split('"', 1)[1].rsplit('"', 1)[0]
        p = inner.split(",")
        if len(p) < 4 or not p[3]:
            return None
        price = float(p[3])
        prev = float(p[2] or 0)
        if price <= 0 and prev > 0:
            price = prev
        if price <= 0:
            return None
        chg = ((price - prev) / prev * 100) if prev else 0.0
        return {
            "price": price,
            "chg": chg,
            "name": p[0],
            "turnover": 0.0,
            "volume_ratio": 0.0,
        }
    except Exception:
        return None


def fetch_kline(code: str, lmt: int = 160) -> list[dict]:
    """前复权日K（腾讯主源，新浪兜底）。字段: date open close high low vol(手)。"""
    sym = tx_symbol(code)
    try:
        d = http_json(
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{lmt},qfq"
        )
        node = (d.get("data") or {}).get(sym) or {}
        kk = node.get("qfqday") or node.get("day") or []
        bars = []
        for k in kk:
            try:
                bars.append({
                    "date": str(k[0]),
                    "open": float(k[1]), "close": float(k[2]),
                    "high": float(k[3]), "low": float(k[4]),
                    "vol": float(k[5]),
                })
            except (ValueError, IndexError):
                continue
        if len(bars) >= 30:
            return bars
    except Exception:
        pass
    # 新浪兜底
    try:
        d = http_json(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={lmt}"
        )
        bars = []
        for k in d or []:
            bars.append({
                "date": k["day"], "open": float(k["open"]), "close": float(k["close"]),
                "high": float(k["high"]), "low": float(k["low"]), "vol": float(k["volume"]),
            })
        if len(bars) >= 30:
            return bars
    except Exception:
        pass
    raise RuntimeError(f"kline unavailable for {code}")


EM_UT = "b2884a393a59ad64002292a3e90d46a5"
_EM_FLOW_DOWN = False  # 东财 daykline 连续失败后本次进程直接走新浪兜底


def _flow_fresh(out: list[dict]) -> bool:
    """资金流最近一天不应早于 20 天前（防止拿到旧序列/停牌序列）。"""
    if not out:
        return False
    try:
        latest = datetime.strptime(out[-1]["date"], "%Y-%m-%d")
        return 0 <= (datetime.now() - latest).days <= 20
    except (ValueError, TypeError):
        return False


def fetch_fund_flow(code: str, days: int = FUND_DAYS + 6) -> list[dict]:
    """
    逐日主力资金流 [{date, main(元)}, ...]（升序）。
    主源: 东财 daykline（需 ut 参数）；兜底: 新浪资金流。
    """
    f2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    global _EM_FLOW_DOWN
    if not _EM_FLOW_DOWN:
        try:
            url = (
                "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
                f"?lmt=0&klt=101&secid={secid(code)}&fields1=f1,f2,f3,f7&fields2={f2}&ut={EM_UT}"
            )
            d = (http_json(url) or {}).get("data") or {}
            out = []
            for line in (d.get("klines") or []):
                p = line.split(",")
                if len(p) >= 2 and p[0]:
                    out.append({"date": p[0], "main": float(p[1])})
            out.sort(key=lambda x: x["date"])
            if _flow_fresh(out):
                return out[-days:]
            _EM_FLOW_DOWN = True
        except Exception:
            _EM_FLOW_DOWN = True
    # 新浪兜底：主力 = 超大单(r0_net) + 大单(r1_net)，返回为倒序（新→旧）
    try:
        d = http_json(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"MoneyFlow.ssl_qsfx_zjlrqs?daima={tx_symbol(code)}"
        )
        out = []
        for k in d or []:
            out.append({
                "date": str(k.get("opendate", ""))[:10],
                "main": float(k.get("r0_net") or 0) + float(k.get("r1_net") or 0),
            })
        out.sort(key=lambda x: x["date"])
        if _flow_fresh(out):
            return out[-days:]
    except Exception:
        pass
    ak_flow = _fund_flow_akshare(code, days)
    if ak_flow:
        return ak_flow
    return []


def _fund_flow_akshare(code: str, days: int) -> list[dict]:
    """
    AKShare 个股资金流兜底。
    stock_individual_fund_flow → 东财 data.eastmoney.com 资金流向（非 push2）。
    若该调用仍打到东财并失败，返回空列表，评分走「无数据」而非中止扫描。
    """
    mkt = "SH" if code.startswith(("6", "9", "5")) else ("BJ" if code.startswith(("4", "8")) else "SZ")
    df = _ak_call("stock_individual_fund_flow", stock=code, market=mkt.lower())
    out = []
    for row in _df_records(df):
        date = str(_pick(row, "日期", "date") or "")[:10]
        main = _to_float(_pick(row, "主力净流入-净额", "主力净流入", "main"))
        if date:
            out.append({"date": date, "main": main})
    out.sort(key=lambda x: x["date"])
    if _flow_fresh(out):
        return out[-days:]
    return []


# 概念板块榜单里的"伪概念"（涨停复盘/风格/指数成分类），不计入热点
JUNK_BOARD = re.compile(
    r"昨日|涨停|连板|炸板|破板|一字|新高|热股|题材股|强势|活跃|微盘|低价|高价|百元|"
    r"重仓|预盈|预亏|ST|摘帽|转债|富时|MSCI|标普|罗素|沪股通|深股通|融资融券|"
    r"专精特新|高送转|破净|高股息|B股|AB股|中证|沪深300|深成|上证|权重|基金|社保|"
    r"险资|QFII|信托|板块$|股$|个股$"
)


def fetch_concept_boards() -> list[dict]:
    """拉取概念板块全表（按当日涨幅排序）。东财失败则 AKShare 新浪/同花顺降级。"""
    boards = _concept_boards_em()
    if boards:
        return boards
    boards = _concept_boards_akshare()
    if boards:
        _ak_fill_hot_members(boards)
        return boards
    return []


def _concept_boards_em() -> list[dict]:
    """东财 push2 clist 概念板块。失败返回空列表。"""
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=80&po=1&np=1&fltt=2&invt=2"
            "&fid=f3&fs=m:90+t:3+f:!50&fields=f3,f8,f12,f14,f62,f104,f105,f109"
        )
        d = (http_json(url) or {}).get("data") or {}
        boards = []
        for b in d.get("diff") or []:
            try:
                name = str(b["f14"]).strip()
                if JUNK_BOARD.search(name):
                    continue
                boards.append({
                    "code": b["f12"], "name": name,
                    "chg1": b.get("f3") or 0, "chg5": b.get("f109") or 0,
                    "main": b.get("f62") or 0,
                    "up": b.get("f104") or 0, "down": b.get("f105") or 0,
                })
            except (KeyError, TypeError):
                continue
        boards = [b for b in boards if (b["up"] + b["down"]) >= 5]
        boards.sort(key=lambda b: b["chg1"], reverse=True)
        return boards
    except Exception:
        return []


def _concept_boards_akshare() -> list[dict]:
    """
    概念板块 AKShare 兜底（不用 stock_board_concept_name_em，其底层仍是 push2）。
    优先 stock_sector_spot('概念') → 新浪；再试 stock_fund_flow_concept('即时') → 同花顺。
    """
    boards: list[dict] = []
    rows = _df_records(_ak_call("stock_sector_spot", indicator="概念"))
    if not rows:
        rows = _df_records(_ak_call("stock_sector_spot", "概念"))
    if not rows:
        rows = _df_records(_ak_call("stock_sector_spot", indicator="concept"))
    for i, row in enumerate(rows):
        name = str(_pick(row, "板块", "name", "行业") or "").strip()
        if not name or JUNK_BOARD.search(name):
            continue
        chg = _to_float(_pick(row, "涨跌幅", "行业-涨跌幅", "chg1"))
        n = _to_float(_pick(row, "公司家数", "up"), 0)
        label = str(_pick(row, "label", "代码") or "")
        boards.append({
            "code": label or f"sina-{i}",
            "name": name,
            "chg1": chg, "chg5": 0,
            "main": 0, "up": n, "down": 0,
            "label": label,
        })
    if not boards:
        for i, row in enumerate(_df_records(_ak_call("stock_fund_flow_concept", symbol="即时"))):
            name = str(_pick(row, "行业", "板块名称", "板块") or "").strip()
            if not name or JUNK_BOARD.search(name):
                continue
            chg = _to_float(_pick(row, "行业-涨跌幅", "涨跌幅", "阶段涨跌幅"))
            n = _to_float(_pick(row, "公司家数"), 0)
            boards.append({
                "code": f"ths-{i}", "name": name,
                "chg1": chg, "chg5": 0, "main": 0,
                "up": n, "down": 0,
            })
    boards.sort(key=lambda b: b["chg1"], reverse=True)
    return boards


def _ak_fill_hot_members(boards: list[dict]) -> None:
    """用热点板块成分股建立 代码→板块名 反查表，供 F10 失败时匹配题材。"""
    global _hot_members
    mapping: dict[str, list[str]] = {}
    for b in boards[:HOT_TOP_N]:
        name = b.get("name") or ""
        label = b.get("label") or ""
        recs = []
        if label:
            recs = _df_records(_ak_call("stock_sector_detail", sector=label))
        if not recs and name:
            recs = _df_records(_ak_call("stock_board_concept_cons_ths", symbol=name))
        for row in recs:
            raw = str(_pick(row, "代码", "code", "股票代码") or "")
            code = "".join(ch for ch in raw if ch.isdigit())[-6:]
            if len(code) != 6:
                continue
            mapping.setdefault(code, [])
            if name and name not in mapping[code]:
                mapping[code].append(name)
    _hot_members = mapping


def fetch_hot_topics(topn: int = HOT_TOP_N) -> tuple[list[dict], set[str]]:
    """
    热点概念板块 = 当日涨幅榜前 N。
    返回 (板块列表, 板块名集合)；接口不可用时返回空列表（不做静态兜底冒充「热门板块」）。
    """
    boards = fetch_concept_boards()
    if boards:
        hot = [dict(b) for b in boards[:topn]]
        for b in hot:
            b["matched"] = False
        return hot, {b["name"] for b in hot}
    return [], set()


def _match_hot(concepts: list[str], hot_names: set[str]) -> list[str]:
    """概念名与热点名匹配：精确优先，其次双向包含（兜底用）。"""
    hits = [n for n in concepts if n in hot_names]
    for n in concepts:
        if n in hits:
            continue
        for h in hot_names:
            if (len(h) > 2 and h in n) or (len(n) > 2 and n in h):
                hits.append(n)
                break
    return hits


def fetch_concepts(code: str) -> list[str]:
    """个股所属板块/概念。主源东财 F10；失败则用本轮热点成分股反查（AKShare 新浪/同花顺）。"""
    try:
        mkt = "SH" if code.startswith(("6", "9", "5")) else ("BJ" if code.startswith(("4", "8")) else "SZ")
        d = http_json(
            f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={mkt}{code}",
            timeout=12,
        )
        names = []
        for x in d.get("ssbk") or []:
            n = str(x.get("BOARD_NAME") or "").strip()
            if n:
                names.append(n)
        if names:
            return names
    except Exception:
        pass
    return list(_hot_members.get(code, []))


def fetch_holder(code: str) -> dict | None:
    """股东户数（最近两期环比 %）。数据按财报期披露，作为筹码集中度代理。"""
    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_HOLDERNUM_DET&columns=SECURITY_CODE,END_DATE,HOLDER_NUM,"
            "PRE_HOLDER_NUM,HOLDER_NUM_RATIO,AVG_HOLD_NUM&"
            f"filter=(SECURITY_CODE%3D%22{code}%22)&pageNumber=1&pageSize=2&"
            "sortTypes=-1&sortColumns=END_DATE"
        )
        d = (http_json(url) or {}).get("result") or {}
        rows = d.get("data") or []
        if rows:
            return {
                "end_date": str(rows[0].get("END_DATE", ""))[:10],
                "ratio": (rows[0].get("HOLDER_NUM_RATIO") or 0),  # 环比 %
                "holder_num": rows[0].get("HOLDER_NUM") or 0,
                "avg_hold": rows[0].get("AVG_HOLD_NUM") or 0,
            }
    except Exception:
        pass
    return _holder_akshare(code)


def _holder_akshare(code: str) -> dict | None:
    """
    AKShare 股东户数兜底。
    stock_zh_a_gdhs('最新') → 东财 datacenter 全市场快照（非 push2，进程内缓存一次）；
    再试 stock_zh_a_gdhs_detail_em(symbol) 个股明细。失败则返回 None，控盘按中性给分。
    """
    global _ak_holder_map
    if _ak_holder_map is None:
        _ak_holder_map = {}
        df = _ak_call("stock_zh_a_gdhs", symbol="最新")
        for row in _df_records(df):
            raw = str(_pick(row, "代码", "SECURITY_CODE", "股票代码") or "")
            c = "".join(ch for ch in raw if ch.isdigit())[-6:]
            if len(c) != 6:
                continue
            _ak_holder_map[c] = {
                "end_date": str(_pick(row, "统计截止日", "END_DATE", "截止日期") or now_str()[:10])[:10],
                "ratio": _to_float(_pick(row, "股东户数-增减比例", "HOLDER_NUM_RATIO", "增减比例")),
                "holder_num": _to_float(_pick(row, "股东户数", "HOLDER_NUM", "股东户数-本次")),
                "avg_hold": _to_float(_pick(row, "户均持股数", "AVG_HOLD_NUM", "平均持股")),
            }
    if code in _ak_holder_map:
        return _ak_holder_map[code]
    recs = _df_records(_ak_call("stock_zh_a_gdhs_detail_em", symbol=code))
    if not recs:
        return None
    row = recs[0]
    return {
        "end_date": str(_pick(row, "股东户数统计-截止日期", "END_DATE", "截止日期") or now_str()[:10])[:10],
        "ratio": _to_float(_pick(row, "股东户数-增减比例", "HOLDER_NUM_RATIO", "增减比例")),
        "holder_num": _to_float(_pick(row, "股东户数-本次", "HOLDER_NUM")),
        "avg_hold": _to_float(_pick(row, "户均持股-本次", "AVG_HOLD_NUM")),
    }


# --------------------------------------------------------------------------- #
# 四条件计算
# --------------------------------------------------------------------------- #
def compute_volume(bars: list[dict]) -> dict:
    """连续倍量天数 + 当日量比（相对前5日均量）。"""
    vols = [b["vol"] for b in bars]
    n = len(vols)
    ratios = [0.0] * n
    for i in range(n):
        if i >= 5:
            avg5 = sum(vols[i - 5:i]) / 5
            ratios[i] = round(vols[i] / avg5, 3) if avg5 > 0 else 0.0
    run = best = 0
    for r in ratios[-10:]:
        if r >= VOL_MULT:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return {
        "volume_days": best,                      # 近10日内最长连续倍量天数
        "volume_ratio": ratios[-1] if n >= 6 else 0.0,
        "ratios": ratios,
    }


def compute_fund(ffs: list[dict]) -> dict:
    """近5日主力净流入（万元）、流入天数、状态。"""
    last = ffs[-FUND_DAYS:] if ffs else []
    main5 = sum(x["main"] for x in last)
    days = sum(1 for x in last if x["main"] > 0)
    if not last:
        state = "无数据"
    elif main5 > 0 and days >= FUND_INFLOW_REQ:
        state = "流入"
    elif main5 > 0:
        state = "偏流入"
    else:
        state = "流出"
    return {
        "fund_5d": round(main5 / 10000, 1) if last else None,  # 万元
        "inflow_days": days,
        "fund_state": state,
        "fund_5d_str": f"{'%+.0f' % (main5 / 10000)}万" if last else "—",
    }


def compute_control(turnover: float, holder: dict | None) -> dict:
    """控盘度：股东户数环比为主，换手率过高降级。"""
    if holder and holder.get("ratio") is not None:
        r = holder["ratio"]
        if r <= HOLDER_HIGH:
            lvl = "高"
        elif r <= HOLDER_MID:
            lvl = "中"
        else:
            lvl = "低"
        note = f"户数环比{r:+.1f}%"
    else:
        lvl = "中"
        note = "无户数数据"
    if turnover >= TURNOVER_CAP and lvl == "高":
        lvl = "中"
        note += f" (换手{turnover:.1f}%偏高)"
    return {"control": lvl, "holder_ratio": holder["ratio"] if holder else None,
            "control_note": note, "holder_date": holder["end_date"] if holder else None}


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #
def score_row(row: dict) -> dict:
    """按四条件打分（各 25 分）。币圈无「热点题材/主力控盘」，改用 3 条件口径（箱体/倍量/试盘）。"""
    flags: list[list] = []
    pts = 0
    is_crypto = row.get("market") == "crypto"

    vd, vr = int(row.get("volume_days") or 0), float(row.get("volume_ratio") or 0)
    tests = int(row.get("tests") or 0)
    flow, ctrl = row.get("fund_state", ""), row.get("control", "")

    if is_crypto:
        # 币圈 3 条件：倍量(0-34) + 试盘(0-33) + 24h涨幅强度(0-33)，满分 100
        if vd >= VOL_DAYS_REQ and vr >= VOL_MULT:
            pts += 34
            flags.append([f"倍量{vd}日", 1])
        elif vd >= 2 or vr >= 1.5:
            pts += 17
            flags.append([f"放量不足({vd}日)", 0])
        else:
            flags.append([f"量能弱({vd}日)", 0])
        if tests >= 3:
            pts += 33
            flags.append([f"试盘{tests}次", 1])
        elif tests >= 2:
            pts += 16
            flags.append([f"试盘{tests}次", 0])
        else:
            flags.append([f"试盘{tests}次", 0])
        chg = float(row.get("chg") or 0)
        if chg >= 10:
            pts += 33
            flags.append(["24h强劲", 1])
        elif chg >= 5:
            pts += 16
            flags.append(["24h一般", 0])
        else:
            flags.append(["24h偏弱", 0])
    else:
        # A股四条件（各 25 分）
        if row.get("theme_ok"):
            pts += 25
            flags.append(["热点题材", 1])
        else:
            flags.append(["题材弱/非热点", 0])
        if vd >= VOL_DAYS_REQ and vr >= VOL_MULT:
            pts += 25
            flags.append([f"倍量{vd}日", 1])
        elif vd >= 2 or vr >= 1.5:
            pts += 12
            flags.append([f"放量不足3日({vd}日)", 0])
        else:
            flags.append([f"量能未达标({vd}日)", 0])
        if flow == "流入" and ctrl == "高":
            pts += 25
            flags.append(["资金流入+高控盘", 1])
        elif flow == "流入":
            pts += 15
            flags.append(["资金流入/控盘中", 1])
        else:
            flags.append(["资金/控盘弱", 0])
        if tests >= 3:
            pts += 25
            flags.append([f"试盘{tests}次", 1])
        elif tests >= 2:
            pts += 10
            flags.append([f"试盘{tests}次", 0])
        else:
            flags.append([f"试盘{tests}次", 0])

    if pts >= 85:
        mode = "达标关注"
    elif pts >= 70:
        mode = "突破观察"
    elif pts >= 50:
        mode = "观察"
    else:
        mode = "箱内/排除"

    row = dict(row)
    row["score"] = pts
    row["flags"] = [f[0] for f in flags]
    row["flag_pairs"] = flags
    row["mode"] = mode
    row["qualified"] = pts >= 85
    return row


# --------------------------------------------------------------------------- #
# 扫描主流程
# --------------------------------------------------------------------------- #
def load_pool() -> list[dict]:
    if POOL_FILE.exists():
        raw = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        stocks = raw.get("stocks") or []
        out = []
        for s in stocks:
            if not str(s.get("code", "")).strip():
                continue
            out.append({
                "code": str(s["code"]).strip(),
                "name": str(s.get("name") or "").strip(),
                "theme": str(s.get("theme") or "").strip(),
            })
        return out
    return []


def analyze(code: str, name: str, theme_hint: str,
            hot_names: set[str], box_mode: str | None = None) -> dict:
    """分析单只股票：拉全量数据 + 四条件计算。"""
    mode = normalize_box_mode(box_mode or load_box_mode())
    q = fetch_quote(code)
    bars = fetch_kline(code)
    ffs = fetch_fund_flow(code)
    holder = fetch_holder(code)

    vol = compute_volume(bars)
    box = compute_box(bars, mode=mode)
    fund = compute_fund(ffs)
    ctrl = compute_control(q["turnover"], holder)

    # 热点判定：个股概念名与热点板块名匹配（榜单为空则诚实判 False，不做静态兜底）
    hot_boards: list[str] = []
    concepts: list[str] = []
    try:
        concepts = fetch_concepts(code)
        hot_boards = _match_hot(concepts, hot_names)
    except Exception:
        concepts = []
    theme_ok = bool(hot_boards)

    row = {
        "code": code,
        "name": q.get("name") or name or code,
        "price": q["price"],
        "chg": q["chg"],
        "turnover": q["turnover"],
        "volume_ratio": max(vol["volume_ratio"], q["volume_ratio"]),  # 当日量比（已修正的日内量能）
        "volume_ratio_raw": vol["volume_ratio"],
        "volume_days": vol["volume_days"],
        **box_row_fields(box, mode),
        **flag_row_fields(detect_high_flag(bars)),
        **trendline_row_fields(detect_trendline(bars)),
        "fund_5d": fund["fund_5d"],
        "inflow_days": fund["inflow_days"],
        "fund_state": fund["fund_state"],
        "control": ctrl["control"],
        "holder_ratio": ctrl["holder_ratio"],
        "control_note": ctrl["control_note"],
        "holder_date": ctrl["holder_date"],
        "theme_hint": theme_hint or "",
        "theme_ok": theme_ok,
        "hot_boards": hot_boards,
        "concepts": concepts[:12],
        "bar_date": bars[-1]["date"] if bars else "",
        "as_of_quote": now_str(),
    }
    return score_row(row)


def run_scan(network: bool = True, progress=None, workers: int | None = None) -> list[dict]:
    """执行扫描，写 data/watchlist.json，返回候选行。自选池与全市场/币圈一样按 scan_workers 并发。"""
    pool = load_pool()
    box_mode = load_box_mode()
    pattern_family = load_pattern_family()
    n_workers = resolve_scan_workers(workers)
    hot_topics, hot_names = ([], set())
    if network:
        emit_progress(progress, "拉取热点概念板块…", phase="hot", done=0, total=len(pool))
        hot_topics, hot_names = fetch_hot_topics()

    total = len(pool)
    emit_progress(progress, f"自选池 {total} 只，{n_workers} 线程并发分析…",
                  phase="analyze", done=0, total=total)

    def one(s: dict) -> dict:
        try:
            return analyze(s["code"], s["name"], s["theme"], hot_names, box_mode=box_mode)
        except Exception as e:
            return score_row({
                "code": s["code"], "name": s["name"] or s["code"],
                "price": None, "chg": None, "theme_hint": s.get("theme", ""),
                "theme_ok": False, "volume_days": 0, "volume_ratio": 0.0,
                **box_row_fields(None, box_mode),
                **flag_row_fields(None),
                **trendline_row_fields(None),
                "fund_state": "无数据", "control": "—", "error": str(e)[:120],
                "flags": [f"数据错误:{str(e)[:40]}", 0],
            })

    rows, done = [], 0
    lock = threading.Lock()
    if total:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(one, s): s for s in pool}
            for f in as_completed(futs):
                rows.append(f.result())
                s = futs[f]
                with lock:
                    done += 1
                    n_done = done
                emit_progress(
                    progress,
                    f"[{n_done}/{total}] 分析 {s.get('code', '')} {s.get('name', '')}",
                    phase="analyze", done=n_done, total=total,
                )

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("chg") or 0), reverse=True)
    payload = decorate_scan_payload({
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "pool",
        "pool_size": len(rows),
        "box_mode": box_mode,
        "pattern_family": pattern_family,
        "hot_topics": [{"code": b["code"], "name": b["name"],
                        "chg1": b["chg1"], "chg5": b["chg5"]} for b in hot_topics],
        "candidates": rows,
    })
    DATA.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


# --------------------------------------------------------------------------- #
# 全市场扫描（沪深 A 股）
# --------------------------------------------------------------------------- #
UNIVERSE_FILE = DATA / "universe.json"
MKT_CACHE_FILE = DATA / "mkt_cache.json"
UNIVERSE_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 深主板A+创业板+沪主板A+科创板
MARKET_TOP = 200       # 默认深度计算候选数
SCREEN_VR = 1.2        # 粗筛：量比 ≥ 1.2 且当日上涨未涨停
CACHE_LOCK = threading.Lock()
_mkt_cache: dict | None = None


def _mkt_cache_load() -> dict:
    global _mkt_cache
    with CACHE_LOCK:
        if _mkt_cache is None:
            try:
                _mkt_cache = json.loads(MKT_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                _mkt_cache = {}
        return _mkt_cache


def _mkt_cache_save() -> None:
    with CACHE_LOCK:
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            MKT_CACHE_FILE.write_text(json.dumps(_mkt_cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def fetch_universe(force: bool = False) -> list[dict]:
    """沪深全部 A 股列表，按日缓存。主源东财 clist（含量比），兜底新浪 sh_a+sz_a，再兜底 AKShare。"""
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    if not force and UNIVERSE_FILE.exists():
        try:
            d = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            if d.get("as_of") == today and d.get("stocks"):
                return d["stocks"]
        except Exception:
            pass
    stocks = _universe_em() or _universe_sina() or _universe_akshare()
    if not stocks:
        raise RuntimeError("股票清单获取失败（东财 clist / 新浪 sh_a+sz_a / AKShare 均不可用）")
    DATA.mkdir(parents=True, exist_ok=True)
    UNIVERSE_FILE.write_text(
        json.dumps({"as_of": today, "total": len(stocks), "stocks": stocks},
                   ensure_ascii=False), encoding="utf-8")
    return stocks


def _universe_em() -> list[dict] | None:
    """东财 clist 分页拉取（含量比 f10）。失败返回 None。"""
    def page(pn: int):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={UNIVERSE_FS}"
            "&fields=f2,f3,f8,f10,f12,f14,f20"
        )
        last = None
        for i in range(4):                       # 单页重试（应对 502/限流）
            try:
                return (http_json(url, timeout=12) or {}).get("data") or {}
            except Exception as e:
                last = e
                time.sleep(0.8 * (i + 1))
        raise RuntimeError(f"page {pn}: {last}")

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    try:
        stocks, pn, total = [], 1, None
        while True:
            d = page(pn)
            total = d.get("total") or 0
            for b in d.get("diff") or []:
                name = str(b.get("f14") or "").strip()
                px = _f(b.get("f2"))
                if not name or px <= 0:
                    continue
                stocks.append({
                    "code": str(b["f12"]), "name": name,
                    "price": px, "chg": _f(b.get("f3")),
                    "turnover": _f(b.get("f8")), "vr": _f(b.get("f10")),
                    "amount": 0.0, "mv": _f(b.get("f20")),
                })
            if not total or not d.get("diff"):
                break
            if pn * 100 >= total:
                break
            pn += 1
            if pn > 80:
                break
            time.sleep(0.12)
        return stocks if len(stocks) > 3000 else None
    except Exception:
        return None


def _universe_sina() -> list[dict] | None:
    """
    新浪沪深 A 股分页拉取（无量比字段，vr=0）。
    CN VPS 上 node=hs_a 经常不全/失败，改为 sh_a + sz_a 分市场拉取。
    价格：成交价 trade>0 用 trade，否则用结算价 settlement（收盘后 trade 常为 0）。
    """
    try:
        stocks, seen = [], set()
        for node in ("sh_a", "sz_a"):
            try:
                for s in _universe_sina_node(node):
                    if s["code"] in seen:
                        continue
                    seen.add(s["code"])
                    stocks.append(s)
            except Exception:
                continue
        return stocks if len(stocks) > 3000 else None
    except Exception:
        return None


def _universe_sina_node(node: str) -> list[dict]:
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    stocks, page_no = [], 1
    while True:
        d = http_json(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeData?page={page_no}&num=100&sort=symbol&asc=1"
            f"&node={node}&symbol=&_s_r_a=page", timeout=15,
        )
        if not d:
            break
        for b in d:
            sym = str(b.get("symbol") or "")
            if not sym.startswith(("sh", "sz")):
                continue                       # 排除北交所等，仅沪深两市
            if sym.startswith(("sh9", "sz2")):
                continue                       # 排除 B 股
            name = str(b.get("name") or "").strip()
            px = _f(b.get("trade"))
            if px <= 0:
                px = _f(b.get("settlement"))
            if not name or px <= 0:
                continue
            stocks.append({
                "code": str(b.get("code") or sym[2:])[-6:],
                "name": name,
                "price": px,
                "chg": _f(b.get("changepercent")),
                "turnover": _f(b.get("turnoverratio")),
                "vr": 0.0,
                "amount": _f(b.get("amount")),
                "mv": _f(b.get("mktcap")),
            })
        if len(d) < 100:
            break
        page_no += 1
        if page_no > 80:
            break
        time.sleep(0.1)
    return stocks


def _universe_akshare() -> list[dict] | None:
    """
    AKShare 股票清单兜底。
    stock_info_a_code_name → 上交所/深交所官方名单（不走 push2；无实时价，深度计算时再补行情）。
    不用 stock_zh_a_spot_em（底层仍是东财 push2 clist）。
    """
    recs = _df_records(_ak_call("stock_info_a_code_name"))
    stocks = []
    for row in recs:
        raw = str(_pick(row, "code", "证券代码", "代码") or "")
        code = "".join(ch for ch in raw if ch.isdigit())[-6:]
        name = str(_pick(row, "name", "证券简称", "名称") or "").strip()
        if len(code) != 6 or not name:
            continue
        if code.startswith(("9", "2")):
            continue                       # 排除 B 股
        if code.startswith(("4", "8")):
            continue                       # 排除北交所，与新浪口径一致
        stocks.append({
            "code": code, "name": name,
            "price": 0.0, "chg": 0.0, "turnover": 0.0,
            "vr": 0.0, "amount": 0.0, "mv": 0.0,
        })
    return stocks if len(stocks) > 3000 else None


def screen_universe(stocks: list[dict], top: int, pool_codes: set[str]) -> list[dict]:
    """
    粗筛（当日活跃度）。量比可用时按量比排名；否则按 涨幅+换手 强度排名。
    池内标的保送。
    """
    base = [s for s in stocks if 0 < s["chg"] < 9.8 and s["price"] > 2]
    has_vr = sum(1 for s in stocks if s["vr"] > 0.05) > 500
    if has_vr:
        cands = [s for s in base if s["vr"] >= SCREEN_VR]
        cands.sort(key=lambda s: (s["vr"], s["chg"]), reverse=True)
    else:
        cands = [s for s in base if 1.5 <= s["turnover"] <= 30]
        cands.sort(key=lambda s: (s["chg"] + min(s["turnover"], 20) * 0.12,
                                  s["amount"]), reverse=True)
    picked = cands[:top]
    have = {s["code"] for s in picked}
    for s in stocks:                     # 自选池保送
        if s["code"] in pool_codes and s["code"] not in have:
            picked.append(s)
            have.add(s["code"])
    return picked


def _cached_concepts(code: str) -> list[str]:
    cache = _mkt_cache_load()
    ent = cache.get("concepts", {}).get(code)
    if ent and ent.get("t"):
        try:
            age = (datetime.now() - datetime.strptime(ent["t"], "%Y-%m-%d")).days
            if age <= 30:
                return ent.get("names", [])
        except ValueError:
            pass
    names = []
    try:
        names = fetch_concepts(code)
    except Exception:
        names = []
    cache.setdefault("concepts", {})[code] = {"t": now_str()[:10], "names": names}
    _mkt_cache_save()
    return names


def _cached_holder(code: str) -> dict | None:
    cache = _mkt_cache_load()
    ent = cache.get("holders", {}).get(code)
    if ent and ent.get("ratio") is not None:
        try:
            age = (datetime.now() - datetime.strptime(ent["end_date"], "%Y-%m-%d")).days
            if age <= 120:               # 财报期披露，季度内复用
                return ent
        except (ValueError, TypeError):
            pass
    h = None
    try:
        h = fetch_holder(code)
    except Exception:
        h = None
    cache.setdefault("holders", {})[code] = h or {"ratio": None, "end_date": now_str()[:10]}
    _mkt_cache_save()
    return h


def analyze_market(s: dict, hot_names: set[str], box_mode: str | None = None) -> dict | None:
    """对粗筛候选做全量四条件计算；数据不足返回 None（不占位）。"""
    try:
        mode = normalize_box_mode(box_mode or load_box_mode())
        code = s["code"]
        price, chg, turnover, vr = s.get("price") or 0, s.get("chg") or 0, s.get("turnover") or 0, s.get("vr") or 0
        if price <= 0:
            try:
                q = fetch_quote(code)
                price = q["price"]
                chg = q["chg"]
                turnover = q.get("turnover") or turnover
                vr = max(vr, q.get("volume_ratio") or 0)
            except Exception:
                pass
        bars = fetch_kline(code)
        if len(bars) < 40:
            return None
        if price <= 0:
            price = bars[-1]["close"]
        ffs = fetch_fund_flow(code)
        holder = _cached_holder(code)
        concepts = _cached_concepts(code)
        hot_boards = _match_hot(concepts, hot_names)
        theme_ok = bool(hot_boards)
        vol = compute_volume(bars)
        box = compute_box(bars, mode=mode)
        fund = compute_fund(ffs)
        ctrl = compute_control(turnover, holder)
        row = {
            "code": code, "name": s.get("name") or code,
            "price": price, "chg": chg, "turnover": turnover,
            "volume_ratio": max(vol["volume_ratio"], vr),
            "volume_ratio_raw": vol["volume_ratio"],
            "volume_days": vol["volume_days"],
            **box_row_fields(box, mode),
            **flag_row_fields(detect_high_flag(bars)),
            **trendline_row_fields(detect_trendline(bars)),
            "fund_5d": fund["fund_5d"],
            "inflow_days": fund["inflow_days"],
            "fund_state": fund["fund_state"],
            "control": ctrl["control"],
            "holder_ratio": ctrl["holder_ratio"],
            "control_note": ctrl["control_note"],
            "holder_date": ctrl["holder_date"],
            "theme_hint": "", "theme_ok": theme_ok,
            "hot_boards": hot_boards, "concepts": concepts,
            "bar_date": bars[-1]["date"],
            "as_of_quote": now_str(),
        }
        return score_row(row)
    except Exception:
        return None


def _save_market(rows: list[dict], stocks: list[dict], hot_topics: list[dict],
                 done: int, total: int, final: bool, box_mode: str | None = None,
                 pattern_family: str | None = None) -> None:
    payload = decorate_scan_payload({
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "market",
        "universe_size": len(stocks),
        "screened": total,
        "scored": len(rows),
        "scanned": done,
        "done": final,
        "box_mode": normalize_box_mode(box_mode or load_box_mode()),
        "pattern_family": normalize_pattern_family(
            pattern_family or load_pattern_family()),
        "hot_topics": [{"code": b["code"], "name": b["name"],
                        "chg1": b["chg1"], "chg5": b["chg5"]} for b in hot_topics],
        "candidates": rows,
    })
    DATA.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_market_scan(full: bool = True, top: int = MARKET_TOP,
                    workers: int | None = None, progress=None,
                    force_universe: bool = False) -> list[dict]:
    """
    全市场扫描。full=True：沪深全部 A 股逐一深度计算（无粗筛）；
    full=False（快扫）：量比粗筛 TOP N 后深度计算。
    workers 默认走 resolve_scan_workers()（4–32，默认 16）。
    """
    n_workers = resolve_scan_workers(workers)
    emit_progress(progress, "拉取沪深 A 股全量清单（首次较慢，此后按日缓存）…",
                  phase="universe", done=0, total=0)
    stocks = fetch_universe(force=force_universe)
    pool_codes = {p["code"] for p in load_pool()}
    box_mode = load_box_mode()
    pattern_family = load_pattern_family()
    if full:
        picked = list(stocks)
        emit_progress(progress, f"全市场 {len(stocks)} 只，全部深度计算（无粗筛）…",
                      phase="analyze", done=0, total=len(picked))
    else:
        picked = screen_universe(stocks, top, pool_codes)
        emit_progress(progress, f"全市场 {len(stocks)} 只 → 快扫粗筛出 {len(picked)} 只候选",
                      phase="analyze", done=0, total=len(picked))

    hot_topics, hot_names = fetch_hot_topics()
    emit_progress(progress, f"热点概念 TOP{len(hot_topics)} 已就绪，形态 {pattern_family}，"
                  f"箱体模式 {box_mode}，开始并发深度计算（{n_workers} 线程）…",
                  phase="analyze", done=0, total=len(picked))

    rows, done = [], 0
    lock = threading.Lock()
    n_picked = len(picked)

    def one(s: dict):
        nonlocal done
        try:
            return analyze_market(s, hot_names, box_mode=box_mode)
        finally:
            with lock:
                done += 1
                n_done = done
                n_rows = len(rows)
                do_save = n_done % 300 == 0
                if do_save:
                    _save_market(sorted(rows, key=lambda r: r.get("score") or 0, reverse=True),
                                 stocks, hot_topics, n_done, n_picked, final=False,
                                 box_mode=box_mode, pattern_family=pattern_family)
            emit_progress(progress, f"深度计算 {n_done}/{n_picked}，已有效 {n_rows} 只",
                          phase="analyze", done=n_done, total=n_picked)

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = [ex.submit(one, s) for s in picked]
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    rows.append(r)
            except Exception:
                pass

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("volume_ratio") or 0,
                             r.get("chg") or 0), reverse=True)
    _save_market(rows, stocks, hot_topics, done, n_picked, final=True,
                 box_mode=box_mode, pattern_family=pattern_family)
    skipped = n_picked - len(rows)
    n_flag = sum(1 for r in rows if r.get("pattern") == "high_flag")
    n_tl = sum(1 for r in rows if r.get("tl_has_line"))
    extra = ""
    if pattern_family == "high_flag":
        extra = f"，旗形 {n_flag} 只"
    elif pattern_family == "trendline":
        extra = f"，趋势线 {n_tl} 只"
    emit_progress(progress, f"完成：有效评分 {len(rows)} 只（数据不足跳过 {skipped} 只），"
                  f"达标 {sum(1 for r in rows if r.get('qualified'))} 只{extra}",
                  phase="save", done=n_picked, total=n_picked)
    return rows


# --------------------------------------------------------------------------- #
# 加密货币（Binance USDT 永续为主，失败后粘性回退 Gate.io USDT 永续）
# --------------------------------------------------------------------------- #
CRYPTO_FILE = DATA / "crypto.json"
BINANCE_FUTURES = "https://fapi.binance.com"
GATE_FUTURES = "https://api.gateio.ws/api/v4/futures/usdt"
_crypto_backend = "binance"  # 本轮扫描内粘性：Binance 一旦失败则整轮改走 Gate


def reset_crypto_backend() -> None:
    global _crypto_backend
    _crypto_backend = "binance"


def norm_crypto_symbol(sym: str) -> str:
    """Gate `BTC_USDT` / Binance `BTCUSDT` → 统一 `BTCUSDT`。"""
    return (sym or "").replace("_", "").replace("-", "").upper()


def gate_contract(sym: str) -> str:
    """`BTCUSDT` → Gate 合约名 `BTC_USDT`。"""
    s = norm_crypto_symbol(sym)
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}_USDT"
    return s


def _mark_crypto_gate() -> None:
    global _crypto_backend
    _crypto_backend = "gate"


def fetch_crypto_tickers() -> list[dict]:
    """USDT 永续全市场 24h 行情。Binance 失败则粘性回退 Gate.io。"""
    if _crypto_backend != "gate":
        try:
            rows = _binance_tickers()
            if rows:
                return rows
        except Exception:
            _mark_crypto_gate()
    return _gate_tickers()


def _binance_tickers() -> list[dict]:
    d = http_json(f"{BINANCE_FUTURES}/fapi/v1/ticker/24hr", timeout=15)
    out = []
    for t in d or []:
        sym = norm_crypto_symbol(str(t.get("symbol") or ""))
        if not sym.endswith("USDT"):
            continue
        try:
            chg = float(t.get("priceChangePercent") or 0)
            price = float(t.get("lastPrice") or 0)
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        out.append({
            "symbol": sym,
            "price": price,
            "chg": chg,
            "quote_volume": float(t.get("quoteVolume") or 0),
            "volume_ratio": 0.0,
            "turnover": 0.0,
        })
    out.sort(key=lambda x: x["chg"], reverse=True)
    return out


def _gate_tickers() -> list[dict]:
    d = http_json(f"{GATE_FUTURES}/tickers", timeout=15)
    out = []
    for t in d or []:
        contract = str(t.get("contract") or "")
        if not contract.endswith("_USDT") and not contract.upper().endswith("USDT"):
            continue
        sym = norm_crypto_symbol(contract)
        if not sym.endswith("USDT"):
            continue
        try:
            chg = float(t.get("change_percentage") or 0)
            price = float(t.get("last") or 0)
        except (ValueError, TypeError):
            continue
        if price <= 0:
            continue
        out.append({
            "symbol": sym,
            "price": price,
            "chg": chg,
            "quote_volume": _to_float(t.get("volume_24h_quote")),
            "volume_ratio": 0.0,
            "turnover": 0.0,
        })
    out.sort(key=lambda x: x["chg"], reverse=True)
    return out


def fetch_crypto_kline(symbol: str, limit: int = CRYPTO_LOOKBACK,
                       interval: str = "1d") -> list[dict]:
    """永续 K 线。Binance 失败后本轮粘性改走 Gate.io。interval 已规范为 4h/8h/1d。"""
    iv = normalize_crypto_interval(interval)
    if _crypto_backend != "gate":
        try:
            bars = _binance_klines(symbol, limit, iv)
            if bars:
                return bars
        except Exception:
            _mark_crypto_gate()
            return _gate_klines(symbol, limit, iv)
    return _gate_klines(symbol, limit, iv)


def _binance_klines(symbol: str, limit: int, interval: str) -> list[dict]:
    iv = normalize_crypto_interval(interval)
    d = http_json(
        f"{BINANCE_FUTURES}/fapi/v1/klines?symbol={norm_crypto_symbol(symbol)}"
        f"&interval={iv}&limit={limit}",
        timeout=15,
    )
    bars = []
    for k in d or []:
        try:
            bars.append({
                "date": format_bar_date(k[0] / 1000, iv),
                "open": float(k[1]), "close": float(k[4]),
                "high": float(k[2]), "low": float(k[3]),
                "vol": float(k[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return bars


def _gate_klines(symbol: str, limit: int, interval: str) -> list[dict]:
    # Gate USDT 永续 candlesticks 与 Binance 一样使用 4h / 8h / 1d 字符串（另有 1h、1m 等）
    iv = normalize_crypto_interval(interval)
    d = http_json(
        f"{GATE_FUTURES}/candlesticks?contract={gate_contract(symbol)}"
        f"&interval={iv}&limit={limit}",
        timeout=15,
    )
    bars = []
    for k in d or []:
        try:
            ts = k.get("t") if isinstance(k, dict) else k[0]
            o = k.get("o") if isinstance(k, dict) else k[1]
            c = k.get("c") if isinstance(k, dict) else k[4]
            h = k.get("h") if isinstance(k, dict) else k[2]
            low = k.get("l") if isinstance(k, dict) else k[3]
            v = k.get("v") if isinstance(k, dict) else k[5]
            bars.append({
                "date": format_bar_date(float(ts), iv),
                "open": float(o), "close": float(c),
                "high": float(h), "low": float(low),
                "vol": float(v or 0),
            })
        except (ValueError, IndexError, TypeError, AttributeError, KeyError):
            continue
    bars.sort(key=lambda b: b["date"])
    return bars


def fetch_tab_kline(code: str, interval: str | None = None,
                    source: str | None = None,
                    limit: int = CRYPTO_LOOKBACK) -> list[dict]:
    """加密货币 Tab 内任意标的的 K 线：永续走币所，其余走 Yahoo。"""
    iv = normalize_crypto_interval(interval or load_crypto_interval())
    src = (source or "").lower()
    if src == "yahoo" or (src != "crypto" and is_yahoo_symbol(code)):
        inst = fetch_yahoo_instrument(code, interval=iv, lookback=limit)
        return list((inst or {}).get("bars") or [])
    return fetch_crypto_kline(code, limit=limit, interval=iv)


def resolve_gold_instrument(tickers: list[dict] | None = None,
                            interval: str = "1d") -> dict | None:
    """
    选 1 只黄金：先看 24h ticker 里的 XAUUSDT/PAXGUSDT，再探测永续 K 线，
    最后 Yahoo GC=F / GLD。全部失败返回 None（扫描跳过黄金，不中止）。
    """
    iv = normalize_crypto_interval(interval)
    ticker_map = {}
    for t in tickers or []:
        sym = norm_crypto_symbol(str(t.get("symbol") or t.get("code") or ""))
        if sym:
            ticker_map[sym] = t
    for sym in GOLD_CRYPTO_SYMBOLS:
        t = ticker_map.get(sym)
        if t and (t.get("price") or 0) > 0:
            return {
                "code": sym, "name": GOLD_DISPLAY_NAME,
                "price": t.get("price"), "chg": t.get("chg"),
                "asset_class": "gold", "market": "crypto", "source": "crypto",
            }
    for sym in GOLD_CRYPTO_SYMBOLS:
        try:
            bars = fetch_crypto_kline(sym, limit=8, interval="1d")
            if len(bars) < 2:
                continue
            px = bars[-1]["close"]
            prev = bars[-2]["close"]
            chg = ((px - prev) / prev * 100.0) if prev else 0.0
            return {
                "code": sym, "name": GOLD_DISPLAY_NAME,
                "price": px, "chg": chg,
                "asset_class": "gold", "market": "crypto", "source": "crypto",
            }
        except Exception:
            continue
    for ysym in GOLD_YAHOO_SYMBOLS:
        try:
            inst = fetch_yahoo_instrument(ysym, interval=iv, lookback=8)
            if not inst or not inst.get("bars"):
                continue
            return {
                "code": ysym, "name": GOLD_DISPLAY_NAME,
                "price": inst.get("price"), "chg": inst.get("chg"),
                "asset_class": "gold", "market": "crypto", "source": "yahoo",
            }
        except Exception:
            continue
    return None


def analyze_crypto(sym: str, price: float, chg: float,
                   bars: list[dict], box_mode: str | None = None,
                   *, name: str | None = None, asset_class: str = "crypto",
                   source: str = "crypto") -> dict | None:
    """复用同一套箱体/倍量/试盘引擎。market 仍为 crypto（看板 Tab）；asset_class 区分币/金/指数/美股。"""
    if len(bars) < 40:
        return None
    mode = normalize_box_mode(box_mode or load_box_mode())
    vol = compute_volume(bars)
    box = compute_box(bars, mode=mode)
    row = {
        "code": sym, "name": name or sym, "market": "crypto",
        "asset_class": asset_class or "crypto",
        "source": source or "crypto",
        "price": price, "chg": chg,
        "turnover": None, "volume_ratio": vol["volume_ratio"],
        "volume_days": vol["volume_days"],
        **box_row_fields(box, mode),
        **flag_row_fields(detect_high_flag(bars)),
        **trendline_row_fields(detect_trendline(bars)),
        # 全球池无 A 股资金流/控盘/热点 → 走币圈 3 条件口径
        "fund_5d": None, "inflow_days": 0, "fund_state": "—",
        "control": "—", "holder_ratio": None, "control_note": "",
        "theme_hint": "", "theme_ok": False, "hot_boards": [], "concepts": [],
        "bar_date": bars[-1]["date"],
        "as_of_quote": now_str(),
    }
    return score_row(row)


def run_crypto_scan(top: int = CRYPTO_TOP_N, workers: int | None = None,
                    progress=None) -> list[dict]:
    """全球池扫描：币 TOP N + 黄金 + 指数 + 美股，复用箱体/旗形/趋势线引擎。"""
    n_workers = resolve_scan_workers(workers)
    reset_crypto_backend()
    box_mode = load_box_mode()
    pattern_family = load_pattern_family()
    interval = load_crypto_interval()
    emit_progress(progress, "拉取 USDT 永续 24h 行情（Binance，失败则 Gate.io）…",
                  phase="universe", done=0, total=0)
    tickers: list[dict] = []
    try:
        tickers = fetch_crypto_tickers()
    except Exception as e:
        emit_progress(progress, f"永续行情失败，仅扫描黄金/指数/美股：{str(e)[:80]}",
                      phase="universe", done=0, total=0)
        tickers = []
    src = "Gate.io" if _crypto_backend == "gate" else "Binance"
    gold = resolve_gold_instrument(tickers, interval=interval)
    pool = build_global_pool(tickers, top_n=top, gold=gold)
    gold_note = ""
    if gold:
        gold_note = f"黄金 {gold.get('code')}({gold.get('source')})"
    else:
        gold_note = "黄金不可用（已跳过）"
    emit_progress(progress, f"{src} TOP{min(top, len(tickers))} + {gold_note} + 指数/美股，"
                  f"共 {len(pool)} 只，周期 {interval}，形态 {pattern_family}，"
                  f"箱体 {box_mode}，开始扫描（{n_workers} 线程）…",
                  phase="analyze", done=0, total=len(pool))

    rows, done = [], 0
    skipped = 0
    lock = threading.Lock()
    n_pool = len(pool)

    def one(item: dict):
        nonlocal done, skipped
        code = item.get("code") or ""
        try:
            source = item.get("source") or "crypto"
            if source == "yahoo" or is_yahoo_symbol(code):
                inst = fetch_yahoo_instrument(code, interval=interval, lookback=CRYPTO_LOOKBACK)
                if not inst:
                    with lock:
                        skipped += 1
                    return None
                bars = inst.get("bars") or []
                name = item.get("name") if item.get("asset_class") in ("gold", "us_index") else (inst.get("name") or item.get("name") or code)
                if item.get("asset_class") == "gold":
                    name = GOLD_DISPLAY_NAME
                px = inst.get("price") if inst.get("price") is not None else item.get("price")
                chg = inst.get("chg") if inst.get("chg") is not None else item.get("chg")
                if px is None and bars:
                    px = bars[-1]["close"]
                if chg is None and len(bars) >= 2 and bars[-2]["close"]:
                    chg = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100.0
                return analyze_crypto(
                    code, float(px or 0), float(chg or 0), bars, box_mode=box_mode,
                    name=name, asset_class=item.get("asset_class") or "us_stock",
                    source="yahoo",
                )
            bars = fetch_crypto_kline(code, limit=CRYPTO_LOOKBACK, interval=interval)
            px = item.get("price")
            chg = item.get("chg")
            if px is None and bars:
                px = bars[-1]["close"]
            if chg is None and len(bars) >= 2 and bars[-2]["close"]:
                chg = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100.0
            row = analyze_crypto(
                code, float(px or 0), float(chg or 0), bars, box_mode=box_mode,
                name=item.get("name") or code,
                asset_class=item.get("asset_class") or "crypto",
                source="crypto",
            )
            if row is None:
                with lock:
                    skipped += 1
            return row
        except Exception:
            with lock:
                skipped += 1
            return None
        finally:
            with lock:
                done += 1
                n_done = done
            emit_progress(progress, f"全球池扫描 {n_done}/{n_pool}",
                          phase="analyze", done=n_done, total=n_pool)

    if n_pool:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(one, t) for t in pool]
            for f in as_completed(futs):
                try:
                    r = f.result()
                    if r:
                        rows.append(r)
                except Exception:
                    pass

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("chg") or 0), reverse=True)

    def _count(cls: str) -> int:
        return sum(1 for r in rows if r.get("asset_class") == cls)

    payload = decorate_scan_payload({
        "as_of": now_str(),
        "strategy": "箱体突破战法",
        "scope": "crypto",
        "universe_size": len(tickers),
        "screened": len(pool),
        "scored": len(rows),
        "scanned": len(pool),
        "skipped": skipped,
        "done": True,
        "box_mode": box_mode,
        "pattern_family": pattern_family,
        "crypto_interval": interval,
        "hot_topics": [],
        "candidates": rows,
        "source": "gate" if _crypto_backend == "gate" else "binance",
        "gold": ({"code": gold["code"], "source": gold.get("source")} if gold else None),
        "pool_counts": {
            "crypto": _count("crypto"),
            "gold": _count("gold"),
            "us_index": _count("us_index"),
            "us_stock": _count("us_stock"),
        },
    })
    DATA.mkdir(parents=True, exist_ok=True)
    CRYPTO_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_progress(progress, f"全球池完成：有效 {len(rows)} 只（跳过 {skipped}），"
                  f"达标 {sum(1 for r in rows if r.get('qualified'))} 只，周期 {interval}",
                  phase="save", done=n_pool, total=n_pool)
    return rows


# --------------------------------------------------------------------------- #
# 输出 / Telegram
# --------------------------------------------------------------------------- #
def fmt_money(wan: float | None) -> str:
    if wan is None:
        return "—"
    if abs(wan) >= 10000:
        return f"{wan / 10000:+.1f}亿"
    return f"{wan:+.0f}万"


def format_alert(rows: list[dict]) -> str:
    lines = [
        f"箱体突破战法扫描  {now_str()}",
        "条件: 热点题材 | 倍量≥3日 | 资金流入+控盘 | 试盘≥3次",
        "",
    ]
    hits = [r for r in rows if r.get("qualified")]
    watch = [r for r in rows if not r.get("qualified") and (r.get("score") or 0) >= 70]

    def item(r: dict) -> list[str]:
        px = f"{r['price']:.2f}" if r.get("price") else "—"
        chg = f"{r['chg']:+.2f}%" if r.get("chg") is not None else ""
        box = f"{r['box_low']}–{r['box_high']}" if r.get("box_low") else "—"
        return [
            f"  {r['name']} {r['code']}  {px} {chg}  评分{r['score']}  {r['mode']}",
            f"    箱体 {box} | 倍量{r.get('volume_days', 0)}日(量比{r.get('volume_ratio', 0):.2f}) | "
            f"{r.get('fund_state', '—')}{fmt_money(r.get('fund_5d'))}/{r.get('control', '—')}控盘 | 试盘{r.get('tests', 0)}次",
        ]

    if not hits and not watch:
        lines.append("本日无达标/观察标的。")
    else:
        if hits:
            lines.append("【达标 ≥85】")
            for r in hits:
                lines += item(r)
            lines.append("")
        if watch:
            lines.append("【观察 70-84】")
            for r in watch:
                lines += item(r)
    lines += ["", "超短线战法，注意仓位与假突破。非投资建议。"]
    return "\n".join(lines)


def _tg_from_config() -> tuple[str, str]:
    """看板 banner 里保存的 Telegram 配置（data/config.json）作为兜底。"""
    try:
        cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
        return str(cfg.get("tg_token") or ""), str(cfg.get("tg_chat") or "")
    except Exception:
        return "", ""


def telegram_send(text: str) -> bool:
    token = os_environ("TG_BOT_TOKEN") or os_environ("TELEGRAM_BOT_TOKEN")
    chat = os_environ("TG_CHAT_ID") or os_environ("TELEGRAM_CHAT_ID")
    if not token or not chat:
        token, chat = _tg_from_config()
    if not token or not chat:
        print("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过推送。", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = HTTP.post(url, json={"chat_id": chat, "text": text,
                                 "disable_web_page_preview": True}, timeout=15)
        ok = bool(r.ok and r.json().get("ok"))
    except Exception as e:
        print("Telegram 请求失败:", e, file=sys.stderr)
        return False
    print("Telegram:", "OK" if ok else (r.text[:200] if r else "no response"))
    return ok


def os_environ(key: str) -> str:
    return os.environ.get(key, "")


def print_table(rows: list[dict]) -> None:
    def cv(v, fmt="{}"):
        return fmt.format(v) if v is not None else "—"
    hdr = f"{'名称':<8}{'代码':<7}{'现价':>8}{'涨跌%':>8}{'箱体':>16}{'倍量日':>6}{'量比':>6}{'资金5日':>10}{'控盘':>5}{'试盘':>5}{'评分':>5} 状态"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        chg = cv(r.get("chg"), "{:+.2f}")
        box = f"{cv(r.get('box_low'), '{:.2f}')}–{cv(r.get('box_high'), '{:.2f}')}"
        print(
            f"{(r.get('name') or '')[:6]:<8}{r['code']:<7}{cv(r.get('price'), '{:.2f}'):>8}{chg:>8}"
            f"{box:>16}{r.get('volume_days', 0):>6}{cv(r.get('volume_ratio'), '{:.2f}'):>6}"
            f"{fmt_money(r.get('fund_5d')):>10}{str(r.get('control', '—')):>5}"
            f"{r.get('tests', 0):>5}{r.get('score', 0):>5}  {r.get('mode', '')}"
        )
        flags = " / ".join(r.get("flags", []))
        if flags:
            print(f"         {'':<8} {flags}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="箱体突破战法扫描（真实数据）")
    ap.add_argument("--push", action="store_true", help="扫描后推送 Telegram")
    ap.add_argument("--test-push", action="store_true", help="发送连通测试消息")
    ap.add_argument("--no-network", action="store_true", help="不联网，只用本地 watchlist 重算评分")
    ap.add_argument("--cron", action="store_true", help="静默模式（仅错误输出 stderr）")
    ap.add_argument("--quiet", action="store_true", help="不打印表格")
    ap.add_argument("--market", action="store_true",
                    help="全市场扫描：沪深全部 A 股逐一深度计算（无粗筛）")
    ap.add_argument("--crypto", action="store_true",
                    help="全球池扫描：USDT 永续 24h 涨幅前 N + 黄金 + 美股指数/个股，箱体逻辑复用")
    ap.add_argument("--quick", action="store_true",
                    help="快扫模式：量比粗筛 TOP N 后深度计算（仅配合 --market）")
    ap.add_argument("--top", type=int, default=MARKET_TOP,
                    help=f"快扫深度计算候选数（默认 {MARKET_TOP}，仅 --quick 生效）")
    ap.add_argument("--workers", type=int, default=None,
                    help=f"并发线程数（默认 {SCAN_WORKERS_DEFAULT}，范围 {SCAN_WORKERS_MIN}–{SCAN_WORKERS_MAX}；"
                         f"亦可用环境变量 SCAN_WORKERS 或 config.json scan_workers）")
    args = ap.parse_args()

    if args.test_push:
        return 0 if telegram_send(f"箱体突破看板连通测试 {now_str()}") else 1

    n_workers = resolve_scan_workers(args.workers)

    def prog(msg: str, **kwargs):
        if args.cron:
            return
        done, total = kwargs.get("done"), kwargs.get("total")
        if done is not None and total:
            try:
                d, t = int(done), int(total)
            except (TypeError, ValueError):
                d, t = 0, 0
            if t > 40 and d not in (0, 1, t) and d % 25 != 0:
                return
        print(msg, flush=True)

    if args.no_network:
        raw = json.loads(WATCH_FILE.read_text(encoding="utf-8")) if WATCH_FILE.exists() else {
            "candidates": []
        }
        rows = [score_row(c) for c in raw.get("candidates", [])]
        payload = dict(raw)
        payload["as_of"] = now_str()
        payload["candidates"] = rows
        decorate_scan_payload(payload)
        WATCH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.crypto:
        rows = run_crypto_scan(top=CRYPTO_TOP_N, workers=n_workers, progress=prog)
    elif args.market:
        rows = run_market_scan(full=not args.quick, top=args.top,
                               workers=n_workers, progress=prog)
    else:
        rows = run_scan(network=True, progress=prog, workers=n_workers)

    if not args.quiet:
        print_table(rows[:30])
        if len(rows) > 30:
            out_file = CRYPTO_FILE if args.crypto else WATCH_FILE
            print(f"... 其余 {len(rows) - 30} 只见 {out_file.name}")
    print(f"已写入 {CRYPTO_FILE if args.crypto else WATCH_FILE}  ({now_str()})")

    if args.push:
        msg = format_alert(rows)
        if not args.cron:
            print(msg)
        if not telegram_send(msg):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
