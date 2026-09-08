#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密货币 Tab 的全球混合池：常量、K 线周期、Yahoo 行情、1h→4h/8h 重采样。

池子构成（可改本文件顶部常量，无需改扫描主流程）：
  - CRYPTO_TOP_N 只 USDT 永续（24h 涨幅，Binance → 粘性 Gate，见 scanner.py）
  - 黄金 1 只（优先交易所黄金永续，其次 Yahoo 期货/ETF）
  - 主要美股指数若干
  - 固定约 20 只美股（US_STOCKS）

K 线周期 crypto_interval ∈ {4h, 8h, 1d}，默认 1d。
Yahoo 对 4h/8h 没有稳定原生周期：拉 1h 再重采样（见 resample_ohlc_hours）。
国内 VPS 上 Yahoo 可能超时：单票失败则跳过，不中止整轮扫描。
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# 可编辑宇宙（全球池）
# --------------------------------------------------------------------------- #
CRYPTO_TOP_N = 20

CRYPTO_INTERVALS = ("4h", "8h", "1d")
DEFAULT_CRYPTO_INTERVAL = "1d"

# 黄金：优先流动性较好的加密黄金永续；都没有再走 Yahoo。
# 运行时只保留 1 行，asset_class=gold，展示名「黄金」。
GOLD_DISPLAY_NAME = "黄金"
GOLD_CRYPTO_SYMBOLS = ("XAUUSDT", "PAXGUSDT")
GOLD_YAHOO_SYMBOLS = ("GC=F", "GLD")  # COMEX 黄金期货 → SPDR 黄金 ETF

# 美股指数（Yahoo 符号）。改列表即改池子。
US_INDICES: tuple[dict, ...] = (
    {"symbol": "^GSPC", "name": "标普500"},
    {"symbol": "^DJI", "name": "道指"},
    {"symbol": "^IXIC", "name": "纳指"},
    {"symbol": "^NDX", "name": "纳斯达克100"},
)

# 固定约 20 只美股大盘（Yahoo 符号；伯克希尔用 BRK-B）。
US_STOCKS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "BRK-B", "JPM", "V",
    "UNH", "XOM", "JNJ", "WMT", "MA",
    "PG", "HD", "COST", "AVGO", "NFLX",
)

YAHOO_TIMEOUT = 10.0
YAHOO_CHART_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)

_YAHOO_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}

_YAHOO_HTTP = requests.Session()
_YAHOO_HTTP.headers.update(_YAHOO_UA)
_YAHOO_HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=1, backoff_factor=0.3, status_forcelist=[502, 503, 504])),
)

_yahoo_symbol_set: set[str] | None = None


def yahoo_symbol_set() -> set[str]:
    """指数 + 美股 + Yahoo 黄金符号，供 K 线路由识别。"""
    global _yahoo_symbol_set
    if _yahoo_symbol_set is None:
        _yahoo_symbol_set = set(US_STOCKS)
        _yahoo_symbol_set.update(x["symbol"] for x in US_INDICES)
        _yahoo_symbol_set.update(GOLD_YAHOO_SYMBOLS)
    return _yahoo_symbol_set


def is_yahoo_symbol(code: str) -> bool:
    """是否走 Yahoo chart（指数 ^、期货 =F、固定美股/黄金列表）。"""
    raw = (code or "").strip()
    if not raw:
        return False
    if raw.startswith("^") or "=" in raw:
        return True
    return raw in yahoo_symbol_set()


def is_crypto_perp_symbol(code: str) -> bool:
    """USDT 永续风格代码（含加密黄金 XAUUSDT / PAXGUSDT）。"""
    s = (code or "").replace("_", "").replace("-", "").upper()
    if s in GOLD_CRYPTO_SYMBOLS:
        return True
    return bool(s.endswith("USDT") and s.isalnum() and len(s) > 4 and "^" not in (code or "") and "=" not in (code or ""))


# --------------------------------------------------------------------------- #
# 周期规范化
# --------------------------------------------------------------------------- #
_INTERVAL_ALIASES = {
    "4h": "4h", "4小时": "4h", "4hr": "4h", "4hour": "4h", "240": "4h",
    "8h": "8h", "8小时": "8h", "8hr": "8h", "8hour": "8h", "480": "8h",
    "1d": "1d", "d": "1d", "day": "1d", "daily": "1d", "1日": "1d", "日": "1d",
    "1day": "1d", "24h": "1d",
}


def normalize_crypto_interval(raw) -> str:
    """把配置/UI 输入规范为 4h | 8h | 1d；非法值回退默认 1d（不打断旧配置）。"""
    if raw is None:
        return DEFAULT_CRYPTO_INTERVAL
    key = str(raw).strip().lower().replace(" ", "")
    return _INTERVAL_ALIASES.get(key, DEFAULT_CRYPTO_INTERVAL)


def interval_hours(interval: str) -> int:
    iv = normalize_crypto_interval(interval)
    return {"4h": 4, "8h": 8, "1d": 24}.get(iv, 24)


def format_bar_date(ts_sec: float, interval: str) -> str:
    """
    K 线 date 字段。1d 仍为 YYYY-MM-DD（与旧币圈/A股日线一致）；
    4h/8h 带时分，避免图上多根 K 线叠成同一天。
    """
    dt = datetime.fromtimestamp(float(ts_sec))
    if normalize_crypto_interval(interval) == "1d":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_bar_datetime(s: str) -> datetime:
    """解析 bars[].date（日线或带时分）。失败则抛 ValueError。"""
    raw = str(s or "").strip().replace("T", " ")
    if raw.endswith("Z"):
        raw = raw[:-1]
    if "+" in raw[10:]:
        raw = raw.split("+", 1)[0].strip()
    if len(raw) >= 16 and raw[13:14] == ":":
        return datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
    if len(raw) >= 10:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    raise ValueError(f"bad bar date: {s!r}")


def resample_ohlc_hours(bars: list[dict], hours: int) -> list[dict]:
    """
    把 1h（或更细）OHLC 合成 N 小时 K 线。

    分桶：按 naive datetime 的小时向下取整到 hours 的倍数
    （4h → 00/04/08/12/16/20；8h → 00/08/16）。
    桶内：open=首根开，high=最高，low=最低，close=末根收，vol=求和。
    未走完的最后一桶也保留（实盘当前 K）。
    hours 非法或 bars 空则原样返回。
    """
    if not bars or hours not in (4, 8):
        return list(bars or [])
    buckets: dict[datetime, dict] = {}
    order: list[datetime] = []
    for b in bars:
        try:
            dt = parse_bar_datetime(b["date"])
            dt = dt.replace(minute=0, second=0, microsecond=0)
            key = dt.replace(hour=(dt.hour // hours) * hours)
            o, h, low, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            vol = float(b.get("vol") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if key not in buckets:
            buckets[key] = {
                "date": key.strftime("%Y-%m-%d %H:%M"),
                "open": o, "high": h, "low": low, "close": c, "vol": vol,
            }
            order.append(key)
        else:
            ent = buckets[key]
            ent["high"] = max(ent["high"], h)
            ent["low"] = min(ent["low"], low)
            ent["close"] = c
            ent["vol"] += vol
    return [buckets[k] for k in order]


# --------------------------------------------------------------------------- #
# 池子组装
# --------------------------------------------------------------------------- #
def _pool_item(code: str, name: str, asset_class: str, source: str,
               price=None, chg=None) -> dict:
    return {
        "code": code,
        "name": name or code,
        "price": price,
        "chg": chg,
        "asset_class": asset_class,
        "market": "crypto",  # 仍走加密货币 Tab / /api/crypto / kline market=crypto
        "source": source,
    }


def build_global_pool(crypto_tickers: list[dict], top_n: int = CRYPTO_TOP_N,
                      gold: dict | None = None) -> list[dict]:
    """
    组装加密货币 Tab 扫描池：涨幅 TOP N 永续 + 黄金 + 指数 + US_STOCKS。

    crypto_tickers 项至少含 symbol / price / chg（与 fetch_crypto_tickers 一致）。
    gold 已解析的一行（可空：本轮黄金全失败则不加）。
    按 code 去重；黄金候选不占用 TOP N 名额。
    """
    n = int(top_n) if top_n else CRYPTO_TOP_N
    n = max(1, n)
    gold_codes = set(GOLD_CRYPTO_SYMBOLS)
    if gold and gold.get("code"):
        gold_codes.add(str(gold["code"]))

    pool: list[dict] = []
    seen: set[str] = set()

    def add(item: dict) -> None:
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            return
        seen.add(code)
        pool.append(item)

    taken = 0
    for t in crypto_tickers or []:
        if taken >= n:
            break
        sym = str(t.get("symbol") or t.get("code") or "").replace("_", "").replace("-", "").upper()
        if not sym or sym in gold_codes:
            continue
        add(_pool_item(
            sym, sym, "crypto", "crypto",
            price=t.get("price"), chg=t.get("chg"),
        ))
        taken += 1

    if gold and gold.get("code"):
        g = dict(gold)
        g.setdefault("name", GOLD_DISPLAY_NAME)
        g.setdefault("asset_class", "gold")
        g.setdefault("market", "crypto")
        g.setdefault("source", g.get("source") or "crypto")
        add(g)

    for idx in US_INDICES:
        add(_pool_item(idx["symbol"], idx["name"], "us_index", "yahoo"))

    for stk in US_STOCKS:
        add(_pool_item(stk, stk, "us_stock", "yahoo"))

    return pool


def static_global_counts() -> dict:
    """文档/测试用：静态侧数量（不含动态 TOP 币）。"""
    return {
        "gold": 1,
        "us_index": len(US_INDICES),
        "us_stock": len(US_STOCKS),
    }


# --------------------------------------------------------------------------- #
# Yahoo chart
# --------------------------------------------------------------------------- #
def yahoo_fetch_plan(interval: str) -> dict:
    """
    Yahoo 请求计划。4h/8h 无稳定原生周期 → interval=1h 再 resample。
    range 尽量覆盖约 200 根目标 K：1d 用 2y；1h 用 6mo（美股仅交易时段）。
    """
    iv = normalize_crypto_interval(interval)
    if iv == "1d":
        return {"yahoo_interval": "1d", "range": "2y", "resample_hours": 0}
    return {"yahoo_interval": "1h", "range": "6mo", "resample_hours": interval_hours(iv)}


def _yahoo_chart_json(symbol: str, y_interval: str, y_range: str) -> dict | None:
    enc = quote(symbol, safe="")
    last_err = None
    for host in YAHOO_CHART_HOSTS:
        url = (
            f"{host}/v8/finance/chart/{enc}"
            f"?interval={y_interval}&range={y_range}&includePrePost=false"
        )
        try:
            r = _YAHOO_HTTP.get(url, timeout=YAHOO_TIMEOUT)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            data = r.json()
            node = (data.get("chart") or {})
            if node.get("error"):
                last_err = str(node.get("error"))[:80]
                continue
            result = node.get("result") or []
            if result:
                return result[0]
        except Exception as e:
            last_err = str(e)[:80]
            continue
    return None


def _bars_from_yahoo_result(result: dict, interval: str) -> list[dict]:
    ts_list = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    opens, highs, lows, closes = quote.get("open") or [], quote.get("high") or [], quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []
    iv = normalize_crypto_interval(interval)
    bars = []
    n = min(len(ts_list), len(opens), len(highs), len(lows), len(closes))
    for i in range(n):
        try:
            o, h, low, c = opens[i], highs[i], lows[i], closes[i]
            if o is None or h is None or low is None or c is None:
                continue
            ts = float(ts_list[i])
            vol = vols[i] if i < len(vols) and vols[i] is not None else 0
            # Yahoo 时间戳为 UTC unix；日线用日历日，日内带时分（UTC）
            if iv == "1d":
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            bars.append({
                "date": date,
                "open": float(o), "high": float(h), "low": float(low), "close": float(c),
                "vol": float(vol or 0),
            })
        except (TypeError, ValueError, IndexError):
            continue
    bars.sort(key=lambda b: b["date"])
    return bars


def _meta_quote(result: dict, bars: list[dict]) -> tuple[str, float | None, float | None]:
    meta = result.get("meta") or {}
    name = str(meta.get("shortName") or meta.get("longName") or meta.get("symbol") or "").strip()
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    try:
        price = float(price) if price is not None else (bars[-1]["close"] if bars else None)
    except (TypeError, ValueError):
        price = bars[-1]["close"] if bars else None
    chg = None
    try:
        if price is not None and prev not in (None, 0, 0.0):
            chg = (float(price) - float(prev)) / float(prev) * 100.0
        elif len(bars) >= 2 and bars[-2]["close"]:
            chg = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        chg = None
    return name, price, chg


def fetch_yahoo_instrument(symbol: str, interval: str = "1d",
                           lookback: int = 200) -> dict | None:
    """
    拉 Yahoo 标的 K 线 + 现价/涨跌。失败返回 None（调用方跳过该票）。

    返回 {code, name, price, chg, bars, source='yahoo'}。
    4h/8h：1h 重采样后再截 lookback 根。
    """
    iv = normalize_crypto_interval(interval)
    plan = yahoo_fetch_plan(iv)
    result = _yahoo_chart_json(symbol, plan["yahoo_interval"], plan["range"])
    if not result:
        return None
    fetch_iv = "1h" if plan["resample_hours"] else iv
    bars = _bars_from_yahoo_result(result, fetch_iv)
    if plan["resample_hours"]:
        bars = resample_ohlc_hours(bars, plan["resample_hours"])
    if lookback and len(bars) > lookback:
        bars = bars[-int(lookback):]
    if not bars:
        return None
    name, price, chg = _meta_quote(result, bars)
    return {
        "code": symbol,
        "name": name or symbol,
        "price": price,
        "chg": chg,
        "bars": bars,
        "source": "yahoo",
    }


def fetch_yahoo_kline(symbol: str, interval: str = "1d",
                      lookback: int = 200) -> list[dict]:
    """仅返回 bars；失败空列表（K 线 API 与扫描共用）。"""
    inst = fetch_yahoo_instrument(symbol, interval=interval, lookback=lookback)
    return list((inst or {}).get("bars") or [])
