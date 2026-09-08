#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全球池股票/指数行情适配器（国内 VPS 实测可用，不依赖 Yahoo）。

主源 → 备份（失败则降级，单票失败返回空，不抛到扫描主流程）：

  美股日K   Sina JSONP US_MinKService.getDailyK
            → akshare.stock_us_daily（若可用）
            → Naver api.stock.naver.com/stock/{SYM}.O|.N/price
  美股报价   Sina hq.sinajs.cn/list=gb_{sym}
  美股指数   同上 JSONP，代码 .INX .DJI .IXIC .NDX
            → Sina znb 报价（仅校验/现价）
            → 东财 push2delay 报价
  日股日K   Naver /stock/{code}.T/price 分页
            → 东财 push2delay 报价 176.{code}
  日经225   Sina gi.finance.sina.com.cn/hq/daily?symbol=NKY
            → znb_NKY 报价
  韩股/KOSPI Naver fchart.stock.naver.com/siseJson.nhn
            → ak.index_global_hist_sina("首尔综合指数")（指数）

统一 bars：{date, open, high, low, close, vol}。
4h/8h：股票/指数无稳定多日分钟历史时回退日K，并带 interval_note。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from global_pool import normalize_crypto_interval

EQUITY_TIMEOUT = 10.0
EQUITY_INTERVAL_NOTE = "股票/指数无稳定 4h/8h 历史，已用日K"

# 东财 push2delay 仅报价（用户明确不要 push2his K 线）
_EM_QUOTE = "https://push2delay.eastmoney.com/api/qt/stock/get"
_SINA_JSONP = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php"
    "/IO.XSRV2.CallbackList/US_MinKService.getDailyK"
)
_SINA_HQ = "https://hq.sinajs.cn/list="
_SINA_GI = "https://gi.finance.sina.com.cn/hq/daily"
_NAVER_PRICE = "https://api.stock.naver.com/stock/{code}/price"
_NAVER_SISE = "https://fchart.stock.naver.com/siseJson.nhn"

_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "*/*",
}
_SINA_HDR = {**_UA, "Referer": "https://finance.sina.com.cn/"}
_NAVER_HDR = {**_UA, "Referer": "https://finance.naver.com/"}

HTTP = requests.Session()
HTTP.headers.update(_UA)
HTTP.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(total=1, backoff_factor=0.25, status_forcelist=[502, 503, 504])),
)

_NAVER_US_SUFFIX = (".O", ".N", ".K")  # NASDAQ / NYSE / AMEX


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: float = EQUITY_TIMEOUT) -> tuple[int, str]:
    """GET 文本。供测试 patch。失败返回 (0, "")，不抛给扫描主流程。"""
    try:
        r = HTTP.get(url, params=params, headers=headers or _UA, timeout=timeout)
        return r.status_code, r.text or ""
    except Exception:
        return 0, ""


def http_json(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: float = EQUITY_TIMEOUT):
    """GET JSON。非 200 或解析失败返回 None。"""
    code, text = http_get(url, params=params, headers=headers, timeout=timeout)
    if code != 200 or not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 数值 / bars
# --------------------------------------------------------------------------- #
def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s or s in (".", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ymd(raw) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s[:8]) if len(s) >= 8 and s[4] not in "-/" else None
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if len(s) >= 10 and s[4] in "-/" and s[7] in "-/":
        return s[:10].replace("/", "-")
    return None


def _bar(date: str, o, h, low, c, vol=0) -> dict | None:
    try:
        o, h, low, c = float(o), float(h), float(low), float(c)
    except (TypeError, ValueError):
        return None
    if not date:
        return None
    return {
        "date": date,
        "open": o, "high": h, "low": low, "close": c,
        "vol": float(vol or 0),
    }


def _sort_trim(bars: list[dict], lookback: int) -> list[dict]:
    out = [b for b in bars if b]
    out.sort(key=lambda b: b["date"])
    # 去重同日，留最后一根
    dedup: dict[str, dict] = {}
    for b in out:
        dedup[b["date"]] = b
    out = [dedup[k] for k in sorted(dedup)]
    if lookback and len(out) > lookback:
        out = out[-int(lookback):]
    return out


def _chg_from_bars(bars: list[dict]) -> tuple[float | None, float | None]:
    if not bars:
        return None, None
    px = bars[-1]["close"]
    chg = None
    if len(bars) >= 2 and bars[-2]["close"]:
        try:
            chg = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100.0
        except (TypeError, ZeroDivisionError):
            chg = None
    return px, chg


def _instrument(code: str, name: str, asset_class: str, source: str,
                bars: list[dict], price=None, chg=None,
                interval: str = "1d", interval_note: str | None = None) -> dict | None:
    if not bars:
        return None
    px, bar_chg = _chg_from_bars(bars)
    return {
        "code": code,
        "name": name or code,
        "price": price if price is not None else px,
        "chg": chg if chg is not None else bar_chg,
        "bars": bars,
        "source": source,
        "asset_class": asset_class,
        "interval": interval,
        "interval_note": interval_note,
        "interval_limited": bool(interval_note),
        "market": "crypto",
    }


# --------------------------------------------------------------------------- #
# Sina 解析
# --------------------------------------------------------------------------- #
def parse_sina_jsonp_daily(text: str) -> list[dict]:
    """解析 US_MinKService.getDailyK JSONP → bars。CallbackList(null) 为空。"""
    raw = (text or "").strip()
    if not raw or "CallbackList(null)" in raw:
        return []
    i, j = raw.find("("), raw.rfind(")")
    if i < 0 or j <= i:
        return []
    inner = raw[i + 1:j].strip()
    if not inner or inner == "null":
        return []
    try:
        data = json.loads(inner)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    bars = []
    for it in data:
        if not isinstance(it, dict):
            continue
        b = _bar(str(it.get("d") or ""), it.get("o"), it.get("h"), it.get("l"),
                 it.get("c"), it.get("v"))
        if b:
            bars.append(b)
    return bars


def parse_sina_hq(text: str) -> dict | None:
    """
    解析 hq.sinajs.cn 美股/环球报价。
    美股 gb_aapl：name, price, chg%, datetime, chg, ...
    环球 znb_NKY：name, price, chg, chg%, ...
    空串视为无效。
    """
    raw = (text or "").strip()
    m = re.search(r'="([^"]*)"', raw)
    if not m:
        return None
    payload = m.group(1)
    if not payload:
        return None
    parts = payload.split(",")
    if len(parts) < 2:
        return None
    name = parts[0].strip()
    price = _num(parts[1])
    if price is None:
        return None
    chg = None
    if len(parts) >= 3:
        # 美股 [2] 是涨跌幅%；znb [2] 是涨跌额、[3] 是涨跌幅
        a, b = _num(parts[2]), _num(parts[3]) if len(parts) > 3 else None
        if a is not None and abs(a) < 80:
            chg = a
        elif b is not None and abs(b) < 80:
            chg = b
        elif a is not None:
            chg = a
    return {"name": name or None, "price": price, "chg": chg}


def parse_sina_gi_daily(payload) -> list[dict]:
    """解析 gi.finance.sina.com.cn/hq/daily JSON。"""
    if not isinstance(payload, dict):
        return []
    rows = ((payload.get("result") or {}).get("data")) or []
    if isinstance(rows, dict):
        return []
    bars = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        b = _bar(str(it.get("d") or ""), it.get("o"), it.get("h"), it.get("l"),
                 it.get("c"), it.get("v"))
        if b:
            bars.append(b)
    return bars


def parse_naver_price_list(payload) -> list[dict]:
    """
    解析 api.stock.naver.com/stock/{code}/price。
    成功为 list[{localTradedAt, openPrice, highPrice, lowPrice, closePrice}, ...]；
    失败为 {code: StockConflict} 或空。
    该接口无成交量 → vol=0。
    """
    if isinstance(payload, dict):
        if payload.get("code") == "StockConflict":
            return []
        rows = payload.get("priceInfos") or payload.get("result") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    bars = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        date = _ymd(it.get("localTradedAt") or it.get("localDate") or it.get("date"))
        b = _bar(
            date or "",
            _num(it.get("openPrice") if it.get("openPrice") is not None else it.get("open")),
            _num(it.get("highPrice") if it.get("highPrice") is not None else it.get("high")),
            _num(it.get("lowPrice") if it.get("lowPrice") is not None else it.get("low")),
            _num(it.get("closePrice") if it.get("closePrice") is not None else it.get("close")),
            _num(it.get("accumulatedTradingVolume") or it.get("volume") or 0) or 0,
        )
        if b:
            bars.append(b)
    return bars


def parse_naver_sise_json(text: str) -> list[dict]:
    """
    解析 fchart.stock.naver.com/siseJson.nhn。
    形如 JS 数组：[['날짜', '시가', ...], ["20240102", o, h, l, c, v, ...], ...]
    列顺序：日期, 开, 高, 低, 收, 量（与部分博客写反的 close/low 不同，以表头为准）。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    # 去 tab/多余空白后当 JSON 用（键都是字符串或数字）
    compact = raw.replace("\t", " ").replace("'", '"')
    try:
        data = json.loads(compact)
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    header = data[0] if data and isinstance(data[0], list) else []
    # 默认：日期 开 高 低 收 量
    idx = {"date": 0, "open": 1, "high": 2, "low": 3, "close": 4, "vol": 5}
    if header and isinstance(header[0], str):
        names = [str(x) for x in header]
        def col(*cands):
            for c in cands:
                if c in names:
                    return names.index(c)
            return None
        i_d = col("날짜", "date")
        i_o = col("시가", "open")
        i_h = col("고가", "high")
        i_l = col("저가", "low")
        i_c = col("종가", "close")
        i_v = col("거래량", "volume")
        if None not in (i_d, i_o, i_h, i_l, i_c):
            idx = {"date": i_d, "open": i_o, "high": i_h, "low": i_l, "close": i_c,
                   "vol": i_v if i_v is not None else 5}
    bars = []
    for row in data[1:]:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        date = _ymd(row[idx["date"]])
        vol_i = idx["vol"]
        vol = row[vol_i] if vol_i is not None and vol_i < len(row) else 0
        b = _bar(date or "", row[idx["open"]], row[idx["high"]], row[idx["low"]],
                 row[idx["close"]], vol)
        if b:
            bars.append(b)
    return bars


def parse_em_quote(payload) -> dict | None:
    """东财 push2delay /qt/stock/get：f43 最新、f58 名称、f60 昨收、f170 涨跌幅。"""
    if not isinstance(payload, dict):
        return None
    d = payload.get("data")
    if not isinstance(d, dict) or d.get("f43") is None:
        return None
    price = _num(d.get("f43"))
    prev = _num(d.get("f60"))
    chg = _num(d.get("f170"))
    if chg is None and price is not None and prev not in (None, 0):
        chg = (price - prev) / prev * 100.0
    name = str(d.get("f58") or "").strip() or None
    if price is None:
        return None
    return {"name": name, "price": price, "chg": chg,
            "open": _num(d.get("f46")), "high": _num(d.get("f44")),
            "low": _num(d.get("f45"))}


# --------------------------------------------------------------------------- #
# 拉取
# --------------------------------------------------------------------------- #
def sina_us_symbol(code: str) -> str:
    """BRK-B → BRK.B；其余大写。指数 .INX 保持原样。"""
    s = (code or "").strip()
    if s.startswith("."):
        return s.upper()
    s = s.replace("_", ".")
    if "-" in s:
        s = s.replace("-", ".")
    return s.upper()


def sina_us_hq_list(code: str) -> str:
    """AAPL → gb_aapl；BRK-B → gb_brk.b。"""
    s = sina_us_symbol(code).lower()
    return f"gb_{s}"


def fetch_sina_us_daily(symbol: str, lookback: int = 200) -> list[dict]:
    """美股/美指日K，Sina JSONP（VPS 实测 AAPL/.INX 可用，range 参数可能被忽略，本地截 lookback）。"""
    sym = sina_us_symbol(symbol)
    code, text = http_get(_SINA_JSONP, params={"symbol": sym}, headers=_SINA_HDR, timeout=12.0)
    if code != 200:
        return []
    return _sort_trim(parse_sina_jsonp_daily(text), lookback)


def fetch_sina_us_quote(symbol: str) -> dict | None:
    lst = sina_us_hq_list(symbol)
    code, text = http_get(_SINA_HQ + lst, headers=_SINA_HDR)
    if code != 200:
        return None
    q = parse_sina_hq(text)
    if q and (q.get("price") or 0) > 0:
        return q
    return None


def fetch_sina_znb_quote(znb: str) -> dict | None:
    code, text = http_get(_SINA_HQ + f"znb_{znb}", headers=_SINA_HDR)
    if code != 200:
        return None
    q = parse_sina_hq(text)
    if q and (q.get("price") or 0) > 0:
        return q
    return None


def fetch_sina_gi_daily(symbol: str, lookback: int = 200) -> list[dict]:
    payload = http_json(_SINA_GI, params={"symbol": symbol, "num": str(max(lookback + 20, 240))})
    return _sort_trim(parse_sina_gi_daily(payload or {}), lookback)


def fetch_naver_price_daily(naver_code: str, lookback: int = 200) -> list[dict]:
    """日股/美股 Naver price 分页。pageSize 实测可用；无效代码 HTTP 409 StockConflict。"""
    bars: list[dict] = []
    page_size = 60
    pages = max(1, (int(lookback) + page_size - 1) // page_size) + 1
    url = _NAVER_PRICE.format(code=quote(naver_code, safe="."))
    for page in range(1, pages + 1):
        payload = http_json(url, params={"pageSize": str(page_size), "page": str(page)},
                            headers=_NAVER_HDR)
        chunk = parse_naver_price_list(payload)
        if not chunk:
            break
        bars.extend(chunk)
        if len(chunk) < page_size:
            break
        if lookback and len(bars) >= lookback + 5:
            break
        time.sleep(0.05)
    return _sort_trim(bars, lookback)


def fetch_naver_sise_daily(symbol: str, lookback: int = 200) -> list[dict]:
    end = datetime.now(timezone.utc).strftime("%Y%m%d")
    # ~ lookback 个交易日 ≈ lookback*1.6 自然日，再留余量
    days = max(400, int(lookback) * 2)
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    code, text = http_get(
        _NAVER_SISE,
        params={"symbol": symbol, "requestType": "1", "startTime": start,
                "endTime": end, "timeframe": "day"},
        headers=_NAVER_HDR,
        timeout=12.0,
    )
    if code != 200:
        return []
    return _sort_trim(parse_naver_sise_json(text), lookback)


def fetch_em_quote(secid: str) -> dict | None:
    payload = http_json(
        _EM_QUOTE,
        params={"secid": secid, "invt": "2", "fltt": "2",
                "fields": "f43,f44,f45,f46,f57,f58,f60,f170"},
        headers=_UA,
    )
    return parse_em_quote(payload or {})


def _ak_us_daily(symbol: str, lookback: int) -> list[dict]:
    try:
        import akshare as ak  # type: ignore
        df = ak.stock_us_daily(symbol=sina_us_symbol(symbol), adjust="")
        if df is None or getattr(df, "empty", True):
            return []
        bars = []
        for _, row in df.iterrows():
            date = _ymd(row.get("date") if hasattr(row, "get") else row["date"])
            b = _bar(date or "", row["open"], row["high"], row["low"], row["close"],
                     row["volume"] if "volume" in row else 0)
            if b:
                bars.append(b)
        return _sort_trim(bars, lookback)
    except Exception:
        return []


def _ak_us_index(symbol: str, lookback: int) -> list[dict]:
    try:
        import akshare as ak  # type: ignore
        df = ak.index_us_stock_sina(symbol=sina_us_symbol(symbol))
        if df is None or getattr(df, "empty", True):
            return []
        bars = []
        for _, row in df.iterrows():
            date = _ymd(row.get("date") if hasattr(row, "get") else row["date"])
            b = _bar(date or "", row["open"], row["high"], row["low"], row["close"],
                     row["volume"] if "volume" in row else 0)
            if b:
                bars.append(b)
        return _sort_trim(bars, lookback)
    except Exception:
        return []


def _ak_global_index(cn_name: str, lookback: int) -> list[dict]:
    try:
        import akshare as ak  # type: ignore
        df = ak.index_global_hist_sina(cn_name)
        if df is None or getattr(df, "empty", True):
            return []
        bars = []
        for _, row in df.iterrows():
            date = _ymd(row.get("date") if hasattr(row, "get") else row["date"])
            b = _bar(date or "", row["open"], row["high"], row["low"], row["close"],
                     row["volume"] if "volume" in row else 0)
            if b:
                bars.append(b)
        return _sort_trim(bars, lookback)
    except Exception:
        return []


def _naver_us_codes(symbol: str) -> list[str]:
    s = sina_us_symbol(symbol).replace(".", ".")
    # BRK.B → BRK.B.N first
    out = []
    for suf in _NAVER_US_SUFFIX:
        out.append(s + suf)
    return out


def fetch_us_stock_bars(symbol: str, lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_sina_us_daily(symbol, lookback)
    if bars:
        return bars, "sina"
    bars = _ak_us_daily(symbol, lookback)
    if bars:
        return bars, "akshare"
    for nv in _naver_us_codes(symbol):
        bars = fetch_naver_price_daily(nv, lookback)
        if bars:
            return bars, "naver"
    return [], ""


def fetch_us_index_bars(symbol: str, lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_sina_us_daily(symbol, lookback)
    if bars:
        return bars, "sina"
    bars = _ak_us_index(symbol, lookback)
    if bars:
        return bars, "akshare"
    return [], ""


def fetch_jp_stock_bars(code: str, lookback: int = 200) -> tuple[list[dict], str]:
    nv = code if str(code).upper().endswith(".T") else f"{code}.T"
    bars = fetch_naver_price_daily(nv, lookback)
    if bars:
        return bars, "naver"
    return [], ""


def fetch_kr_stock_bars(code: str, lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_naver_sise_daily(code, lookback)
    if bars:
        return bars, "naver"
    return [], ""


def fetch_nky_bars(lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_sina_gi_daily("NKY", lookback)
    if bars:
        return bars, "sina"
    bars = _ak_global_index("日经225指数", lookback)
    if bars:
        return bars, "akshare"
    return [], ""


def fetch_kospi_bars(lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_naver_sise_daily("KOSPI", lookback)
    if bars:
        return bars, "naver"
    bars = fetch_sina_gi_daily("KOSPI", lookback)
    if bars:
        return bars, "sina"
    bars = _ak_global_index("首尔综合指数", lookback)
    if bars:
        return bars, "akshare"
    return [], ""


def fetch_kosdaq_bars(lookback: int = 200) -> tuple[list[dict], str]:
    bars = fetch_naver_sise_daily("KOSDAQ", lookback)
    if bars:
        return bars, "naver"
    return [], ""


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def fetch_equity_instrument(ident: dict, interval: str = "1d",
                            lookback: int = 200) -> dict | None:
    """
    按 resolve_symbol 结果拉 K 线。interval≠1d 时股票/指数回退日K 并标记 interval_note。
    ident 至少含 code / name / asset_class。
    """
    code = str(ident.get("code") or "").strip()
    name = str(ident.get("name") or code)
    cls = ident.get("asset_class") or "us_stock"
    iv = normalize_crypto_interval(interval)
    note = EQUITY_INTERVAL_NOTE if iv != "1d" else None
    fetch_iv = "1d"  # 股票/指数日K；4h/8h 诚实回退
    bars: list[dict] = []
    source = ""
    quote = None

    if cls == "us_stock":
        bars, source = fetch_us_stock_bars(code, lookback)
        quote = fetch_sina_us_quote(code)
    elif cls == "us_index":
        bars, source = fetch_us_index_bars(ident.get("sina_symbol") or code, lookback)
        znb = ident.get("znb")
        if znb:
            quote = fetch_sina_znb_quote(znb)
    elif cls == "jp_stock":
        bars, source = fetch_jp_stock_bars(ident.get("naver_code") or code, lookback)
        digits = re.sub(r"\D", "", code)
        quote = fetch_em_quote(f"176.{digits}") if digits else None
    elif cls == "jp_index":
        bars, source = fetch_nky_bars(lookback)
        quote = fetch_sina_znb_quote("NKY")
    elif cls == "kr_stock":
        bars, source = fetch_kr_stock_bars(code, lookback)
    elif cls == "kr_index":
        if (ident.get("naver_code") or code).upper() == "KOSDAQ":
            bars, source = fetch_kosdaq_bars(lookback)
        else:
            bars, source = fetch_kospi_bars(lookback)
            if not quote:
                quote = fetch_sina_znb_quote("KOSPI")
    else:
        return None

    if not bars:
        return None
    if quote:
        name = quote.get("name") or name
    px = (quote or {}).get("price")
    chg = (quote or {}).get("chg")
    inst = _instrument(code, name, cls, source or "equity", bars,
                       price=px, chg=chg, interval=fetch_iv if note else iv,
                       interval_note=note)
    if inst and iv != "1d":
        inst["requested_interval"] = iv
    return inst


def validate_equity(ident: dict) -> tuple[bool, str]:
    """
    校验股票/指数能被对应源解析。已知指数别名允许在源短暂失败时仍通过（代码表内）。
    返回 (ok, reason)。
    """
    cls = ident.get("asset_class")
    code = ident.get("code") or ""
    known = bool(ident.get("known"))
    try:
        if cls == "us_stock":
            q = fetch_sina_us_quote(code)
            if q and (q.get("price") or 0) > 0:
                return True, ""
            bars, _ = fetch_us_stock_bars(code, lookback=8)
            if bars:
                return True, ""
            return (True, "") if known else (False, "新浪/Naver 无此美股")
        if cls == "us_index":
            bars, _ = fetch_us_index_bars(ident.get("sina_symbol") or code, lookback=5)
            if bars:
                return True, ""
            znb = ident.get("znb")
            if znb and fetch_sina_znb_quote(znb):
                return True, ""
            return (True, "") if known else (False, "新浪无此美股指数")
        if cls == "jp_stock":
            nv = ident.get("naver_code") or code
            bars = fetch_naver_price_daily(nv, lookback=3)
            if bars:
                return True, ""
            digits = re.sub(r"\D", "", code)
            if digits and fetch_em_quote(f"176.{digits}"):
                return True, ""
            return (True, "") if known else (False, "Naver 无此日股")
        if cls == "jp_index":
            if fetch_sina_znb_quote("NKY") or fetch_nky_bars(5)[0]:
                return True, ""
            return (True, "") if known else (False, "日经225 源不可用")
        if cls == "kr_stock":
            bars, _ = fetch_kr_stock_bars(code, lookback=5)
            if bars:
                return True, ""
            return (True, "") if known else (False, "Naver 无此韩股")
        if cls == "kr_index":
            nv = (ident.get("naver_code") or code).upper()
            bars, _ = (fetch_kosdaq_bars(5) if nv == "KOSDAQ" else fetch_kospi_bars(5))
            if bars:
                return True, ""
            if nv != "KOSDAQ" and fetch_sina_znb_quote("KOSPI"):
                return True, ""
            return (True, "") if known else (False, "韩指源不可用")
    except Exception as e:
        if known:
            return True, ""
        return False, str(e)[:80]
    return False, "无法识别资产类别"
