#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场标的 Tab 的全球混合池：常量、K 线周期、符号解析、覆盖池、Yahoo 遗留适配。

池子构成（可改本文件顶部常量，无需改扫描主流程）：
  - CRYPTO_TOP_N 只 USDT 永续（24h 涨幅，Binance → 粘性 Gate）
  - 黄金 1 只（优先 Gate XAUT_USDT 现货/永续，其次 XAUUSDT / PAXGUSDT）
  - 美股 20 + 美指/ETF（Gate USDT 永续干净名）+ 日韩代币代理 + 日经/KOSPI 日K 次源

K 线周期 crypto_interval ∈ {4h, 8h, 1d}，默认 1d。
有 Gate 股票代币的标的：优先 USDT 永续干净名（AAPL_USDT / SPX500_USDT，VPS 实测 1d/4h/8h）；
失败再试现货 *X/*G/*ON；不用 3L/3S。无代币的美股/美指走新浪 getMinK；日韩正股无代币时 Sina/Naver 日K。
代币跟踪正股但存在基差，不是交易所官方打印。日韩 Gate 标的为代币代理。
Yahoo 仅作遗留函数保留；扫描主路径不再 Yahoo-first。
覆盖池非空时扫描只扫用户标的，见 data/global_override_pool.json。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import json
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
OVERRIDE_FILE = ROOT / "data" / "global_override_pool.json"

# --------------------------------------------------------------------------- #
# 可编辑宇宙（全球池）
# --------------------------------------------------------------------------- #
CRYPTO_TOP_N = 20

CRYPTO_INTERVALS = ("4h", "8h", "1d")
DEFAULT_CRYPTO_INTERVAL = "1d"

# 黄金：优先 Gate XAUT_USDT（现货 + 永续，不要用 XAU_USDT 当首选）。
GOLD_DISPLAY_NAME = "黄金"
GOLD_CRYPTO_SYMBOLS = ("XAUTUSDT", "XAUUSDT", "PAXGUSDT")
GOLD_SPOT_PAIR = "XAUT_USDT"
GOLD_YAHOO_SYMBOLS = ("GC=F", "GLD")  # 遗留；扫描主路径不再走 Yahoo

# 默认美指/ETF（进默认宇宙）：SPX500 永续，禁止 SPX_USDT（梗币）与 DIA_USDT（加密 DIA）。
US_INDEX_DEFAULT: tuple[dict, ...] = (
    {"symbol": ".INX", "name": "标普500", "znb": "SPX", "sina_symbol": ".INX"},
)
US_ETF_DEFAULT: tuple[dict, ...] = (
    {"symbol": "SPY", "name": "SPY"},
    {"symbol": "QQQ", "name": "QQQ"},
    {"symbol": "IWM", "name": "IWM"},
)
# 覆盖池仍可解析道指 / 纳指综合 / 纳指100（.IXIC 无代币，走新浪）。
US_INDICES: tuple[dict, ...] = US_INDEX_DEFAULT + (
    {"symbol": ".DJI", "name": "道指", "znb": "DJI", "sina_symbol": ".DJI"},
    {"symbol": ".IXIC", "name": "纳指", "znb": "IXIC", "sina_symbol": ".IXIC"},
    {"symbol": ".NDX", "name": "纳斯达克100", "znb": "NDX", "sina_symbol": ".NDX"},
)

# 默认美股 20：VPS 表 Gate USDT 永续干净名（含 ORCL）。覆盖池仍可解析未进宇宙的映射票。
US_STOCKS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
    "META", "TSLA", "COIN", "BABA", "NFLX",
    "AMD", "AVGO", "ARM", "PLTR", "HOOD",
    "MSTR", "JPM", "WMT", "IBM", "ORCL",
)

# 日/韩：默认索尼/三星走 Gate 代币代理；日经/KOSPI 无代币，新浪/Naver 日K 次源。
JP_INDICES: tuple[dict, ...] = (
    {"symbol": "NKY", "name": "日经225", "sina_gi": "NKY", "ak_name": "日经225指数"},
)
KR_INDICES: tuple[dict, ...] = (
    {"symbol": "KOSPI", "name": "KOSPI", "naver_code": "KOSPI", "ak_name": "首尔综合指数"},
)
KR_INDICES_KNOWN: tuple[dict, ...] = KR_INDICES + (
    {"symbol": "KOSDAQ", "name": "KOSDAQ", "naver_code": "KOSDAQ"},
)
JP_STOCKS: tuple[dict, ...] = (
    {"symbol": "6758.T", "name": "索尼"},
)
JP_STOCKS_KNOWN: tuple[dict, ...] = JP_STOCKS + (
    {"symbol": "7203.T", "name": "丰田"},
)
KR_STOCKS: tuple[dict, ...] = (
    {"symbol": "005930", "name": "三星电子"},
)
KR_STOCKS_KNOWN: tuple[dict, ...] = KR_STOCKS + (
    {"symbol": "000660", "name": "SK海力士"},
)

# Gate 股票代币（2026-09 VPS 表）：优先 USDT 永续干净名；现货 *X/*G/*ON 仅回退；跳过 3L/3S。
# 这是代币化加密市场 / 代币代理，与正股有基差；不是纽交所/东证/韩交所官方打印。
# 不映射：JPN225_USDT、DIA_USDT、SPX_USDT、WMTX、TOYOTA。无 NIKKEI/KOSPI 代币。
# Mastercard 无永续；现货 MAX_USDT 为 xStock 回退。日元丰田 7203.T 无 TOYOTA 合约。
GATE_EQUITY_MAP: dict[str, str] = {
    "AAPL": "AAPL_USDT", "MSFT": "MSFT_USDT", "NVDA": "NVDA_USDT",
    "GOOGL": "GOOGL_USDT", "AMZN": "AMZN_USDT", "META": "META_USDT",
    "TSLA": "TSLA_USDT", "COIN": "COIN_USDT", "BABA": "BABA_USDT",
    "NFLX": "NFLX_USDT", "AMD": "AMD_USDT", "AVGO": "AVGO_USDT",
    "ARM": "ARM_USDT", "PLTR": "PLTR_USDT", "HOOD": "HOOD_USDT",
    "MSTR": "MSTR_USDT", "JPM": "JPM_USDT", "WMT": "WMT_USDT",
    "IBM": "IBM_USDT", "ORCL": "ORCL_USDT",
    "BRK-B": "BRKB_USDT", "V": "V_USDT", "UNH": "UNH_USDT",
    "XOM": "XOM_USDT", "JNJ": "JNJ_USDT", "PG": "PG_USDT",
    "HD": "HD_USDT", "COST": "COST_USDT",
    "MA": "MAX_USDT",           # 无永续；现货 Mastercard xStock
    ".INX": "SPX500_USDT",      # 勿用 SPX_USDT
    ".DJI": "US30_USDT",
    ".NDX": "NAS100_USDT",
    "SPY": "SPY_USDT", "QQQ": "QQQ_USDT", "IWM": "IWM_USDT",
    "6758.T": "SONY_USDT",      # 日股代币代理
    "005930": "SAMSUNG_USDT",   # 韩股代币代理
    "000660": "SKHYNIX_USDT",
}

# 现货回退（永续失败或无永续时）。仅列入已核对标尺的 *X / *G，不含 3L/3S。
GATE_SPOT_FALLBACK: dict[str, str] = {
    "AAPL": "AAPLX_USDT", "NVDA": "NVDAX_USDT", "GOOGL": "GOOGLX_USDT",
    "AMZN": "AMZNX_USDT", "META": "METAX_USDT", "TSLA": "TSLAX_USDT",
    "UNH": "UNHX_USDT", "PG": "PGX_USDT", "HD": "HDX_USDT",
    "AVGO": "AVGOX_USDT", "NFLX": "NFLXX_USDT",
    "MSFT": "MSFTG_USDT",
    "SPY": "SPYX_USDT", "QQQ": "QQQX_USDT",
    "COIN": "COINX_USDT", "HOOD": "HOODX_USDT", "MSTR": "MSTRX_USDT",
}

GATE_SPOT_EQUITY = frozenset(GATE_SPOT_FALLBACK.values()) | frozenset({"MAX_USDT", "XAUT_USDT"})

# 覆盖池可解析、但不进默认宇宙。SQQQ 为可选反向 ETF；TM 是丰田 ADR 代币（≠ 7203.T）。
GATE_EXTRA_EQUITY: dict[str, tuple[str, str, str]] = {
    "SQQQ": ("SQQQ_USDT", "SQQQ", "us_index"),
    "TM": ("TM_USDT", "丰田ADR", "us_stock"),
}

US_INDEX_TOKEN_CODES = frozenset(
    {x["symbol"] for x in US_INDICES}
    | {x["symbol"] for x in US_ETF_DEFAULT}
    | {"SQQQ"}
)

# 猜 {TICKER}_USDT 时跳过：与加密货币撞名，或明显不是股票代币。
GATE_GUESS_BLOCKLIST = frozenset({"DIA", "MA"})  # DIA_USDT=crypto DIA；MA_USDT=Mind AI
_LEVERAGED_TAILS = ("3L", "3S", "5L", "5S", "2L", "2S")


def _build_gate_aliases() -> dict[str, str]:
    """AAPL_USDT / AAPLX / TM / SPX500 / SAMSUNG / MAX → 规范代码。"""
    out: dict[str, str] = {}
    def add(alias: str, code: str) -> None:
        if not alias:
            return
        out[alias] = code
        out[alias.upper()] = code
        compact = alias.replace("_", "").replace("-", "")
        out[compact] = code
        out[compact.upper()] = code

    for code, contract in GATE_EQUITY_MAP.items():
        add(contract, code)
        base = contract[:-5] if contract.endswith("_USDT") else contract
        add(base, code)
        if code not in ("MA",) and not code.startswith(".") and not code[:1].isdigit() \
                and not str(code).endswith(".T"):
            ticker = code.replace("-", "").replace(".", "")
            add(f"{ticker}_USDT", code)
    for code, (contract, _name, _cls) in GATE_EXTRA_EQUITY.items():
        add(code, code)
        add(contract, code)
        base = contract[:-5] if contract.endswith("_USDT") else contract
        add(base, code)
    for code, spot in GATE_SPOT_FALLBACK.items():
        add(spot, code)
        base = spot[:-5] if spot.endswith("_USDT") else spot
        add(base, code)
    return out


def _gate_pair_norm(pair: str) -> str:
    s = (pair or "").strip()
    if "_" not in s and s.upper().endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}_USDT"
    return s


def gate_venue_for_pair(pair: str) -> str:
    """spot = xStock 现货 K 线；futures = USDT 永续。"""
    return "spot" if _gate_pair_norm(pair) in GATE_SPOT_EQUITY else "futures"


_GATE_ALIASES = _build_gate_aliases()
_GATE_ALIASES["SPX_USDT"] = ".INX"
_GATE_ALIASES["SPXUSDT"] = ".INX"


def gate_spot_fallback_for(code: str) -> str | None:
    """永续失败时的现货 *X/*G 回退。无则 None。"""
    if not code:
        return None
    return GATE_SPOT_FALLBACK.get(code)


def mapped_asset_class(code: str) -> str:
    """映射表代码的资产类别：美指/ETF → us_index；日韩代币代理 → jp/kr_stock；其余美股。"""
    if not code:
        return "us_stock"
    extra = GATE_EXTRA_EQUITY.get(code)
    if extra:
        return extra[2]
    if code in US_INDEX_TOKEN_CODES:
        return "us_index"
    if str(code).endswith(".T"):
        return "jp_stock"
    if str(code)[:1].isdigit():
        return "kr_stock"
    return "us_stock"


def catalog_display_name(code: str) -> str:
    """默认池 / 已知目录里的展示名。"""
    for idx in US_INDICES:
        if idx["symbol"] == code:
            return idx["name"]
    for etf in US_ETF_DEFAULT:
        if etf["symbol"] == code:
            return etf["name"]
    for stk in JP_STOCKS_KNOWN:
        if stk["symbol"] == code:
            return stk["name"]
    for stk in KR_STOCKS_KNOWN:
        if stk["symbol"] == code:
            return stk["name"]
    extra = GATE_EXTRA_EQUITY.get(code)
    if extra:
        return extra[1]
    return code


def gate_contract_for(code: str) -> str | None:
    """规范代码 → Gate 合约。无映射返回 None（调用方再猜美股 {TICKER}_USDT 或走 Sina）。"""
    if not code:
        return None
    if code in GATE_EQUITY_MAP:
        return GATE_EQUITY_MAP[code]
    extra = GATE_EXTRA_EQUITY.get(code)
    if extra:
        return extra[0]
    return None


def guess_us_gate_contract(code: str) -> str | None:
    """美股 ticker → 候选合约：BRK-B → BRKB_USDT，AAPL → AAPL_USDT。DIA/杠杆后缀不猜。"""
    raw = (code or "").strip()
    if not raw or raw.startswith(".") or raw.endswith(".T") or raw[:1].isdigit():
        return None
    s = raw.replace("-", "").replace(".", "").upper()
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]
    if s in GATE_GUESS_BLOCKLIST:
        return None
    if any(s.endswith(t) for t in _LEVERAGED_TAILS):
        return None
    if not re.fullmatch(r"[A-Z]{1,6}", s):
        return None
    return f"{s}_USDT"


def gate_equity_norm_set() -> set[str]:
    """涨幅榜应排除的股票代币（规范化无下划线）。"""
    out = {_norm_crypto(v) for v in GATE_EQUITY_MAP.values()}
    out |= {_norm_crypto(v[0]) for v in GATE_EXTRA_EQUITY.values()}
    out |= {_norm_crypto(p) for p in GATE_SPOT_EQUITY}
    for code in US_STOCKS:
        t = code.replace("-", "").replace(".", "")
        out.add(_norm_crypto(t + "USDT"))
        out.add(_norm_crypto(t + "XUSDT"))
    for code in GATE_EXTRA_EQUITY:
        out.add(_norm_crypto(code + "USDT"))
        out.add(_norm_crypto(code + "XUSDT"))
    return out


def is_stock_token_ticker(sym: str) -> bool:
    """
    涨幅榜排除：映射表合约、xStock（AAPLXUSDT）、杠杆（AAPL3LUSDT）以及 *G/*ON 现货变体。
    避免股票代币占掉 CRYPTO_TOP_N。
    """
    s = _norm_crypto(sym)
    if not s:
        return False
    if s in gate_equity_norm_set():
        return True
    if not s.endswith("USDT") or len(s) <= 4:
        return False
    base = s[:-4]
    if any(base.endswith(t) for t in _LEVERAGED_TAILS):
        return True
    for suf, n in (("ON", 2), ("X", 1), ("G", 1)):
        if base.endswith(suf) and len(base) > n:
            core = base[:-n]
            if gate_contract_for(core) or core in US_STOCKS or core in GATE_EXTRA_EQUITY:
                return True
    return False


def _with_gate(ident: dict | None) -> dict | None:
    """给 ident 打上 gate_contract / tokenized / gate_venue；有静态映射则 source=gate。"""
    if not ident:
        return ident
    if ident.get("asset_class") in ("crypto", "gold"):
        return ident
    g = ident.get("gate_contract") or gate_contract_for(ident.get("code") or "")
    if g:
        ident["gate_contract"] = g
        ident["tokenized"] = True
        ident["source"] = "gate"
        ident["gate_venue"] = gate_venue_for_pair(g)
        if ident.get("asset_class") in ("jp_stock", "kr_stock", "jp_index", "kr_index"):
            ident["token_role"] = "proxy"
    return ident


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

# 用户输入别名 → 规范代码（resolve_symbol 再填 display / asset_class）
_INDEX_ALIAS: dict[str, str] = {
    "^GSPC": ".INX", "GSPC": ".INX", "SPX": ".INX", ".INX": ".INX",
    "标普500": ".INX", "标普": ".INX",
    "^DJI": ".DJI", "DJI": ".DJI", ".DJI": ".DJI", "道指": ".DJI", "道琼斯": ".DJI",
    "^IXIC": ".IXIC", "IXIC": ".IXIC", ".IXIC": ".IXIC", "纳指": ".IXIC",
    "^NDX": ".NDX", "NDX": ".NDX", ".NDX": ".NDX", "纳斯达克100": ".NDX",
    "日经225指数": "NKY", "日经225": "NKY", "N225": "NKY", "NKY": "NKY",
    ".N225": "NKY", "NIKKEI": "NKY",
    "首尔综合指数": "KOSPI", "KOSPI": "KOSPI", "KS11": "KOSPI",
    "KOSDAQ": "KOSDAQ",
    "黄金": "XAUTUSDT", "XAU": "XAUTUSDT", "XAUT": "XAUTUSDT",
    "XAUTUSDT": "XAUTUSDT", "XAUT_USDT": "XAUTUSDT",
    "XAUUSDT": "XAUUSDT", "XAU_USDT": "XAUUSDT",
    "PAXGUSDT": "PAXGUSDT", "PAXG_USDT": "PAXGUSDT",
    "GC=F": "XAUTUSDT", "GLD": "XAUTUSDT",
}


def yahoo_symbol_set() -> set[str]:
    """遗留：旧 Yahoo 符号集合（美股代码仍在此，供 is_yahoo_symbol 测试）。"""
    global _yahoo_symbol_set
    if _yahoo_symbol_set is None:
        _yahoo_symbol_set = set(US_STOCKS)
        _yahoo_symbol_set.update(x["symbol"] for x in US_INDICES)
        _yahoo_symbol_set.update(x["symbol"] for x in US_ETF_DEFAULT)
        for code in GATE_EQUITY_MAP:
            if mapped_asset_class(code) == "us_stock":
                _yahoo_symbol_set.add(code)
        _yahoo_symbol_set.update(GOLD_YAHOO_SYMBOLS)
        _yahoo_symbol_set.update(("^GSPC", "^DJI", "^IXIC", "^NDX"))
    return _yahoo_symbol_set


def is_yahoo_symbol(code: str) -> bool:
    """遗留检测：指数 ^、期货 =F、固定美股列表。扫描主路径不再据此走 Yahoo。"""
    raw = (code or "").strip()
    if not raw:
        return False
    if raw.startswith("^") or "=" in raw:
        return True
    return raw in yahoo_symbol_set()


def is_crypto_perp_symbol(code: str) -> bool:
    """USDT 永续风格代码（含加密黄金 XAUTUSDT / XAUUSDT / PAXGUSDT）。"""
    s = (code or "").replace("_", "").replace("-", "").upper()
    if s in GOLD_CRYPTO_SYMBOLS:
        return True
    return bool(s.endswith("USDT") and s.isalnum() and len(s) > 4
                and "^" not in (code or "") and "=" not in (code or ""))


def _norm_crypto(sym: str) -> str:
    return (sym or "").replace("_", "").replace("-", "").upper()


def parse_symbol_text(text: str) -> list[str]:
    """textarea：逗号/中文逗号/分号/换行分隔；去掉空项，保序去重。"""
    raw = (text or "").replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    parts: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        for p in line.split(","):
            s = p.strip()
            if not s:
                continue
            key = s.upper()
            if key in seen:
                continue
            seen.add(key)
            parts.append(s)
    return parts


def _ident(code: str, name: str, asset_class: str, source: str, **extra) -> dict:
    out = {
        "code": code,
        "name": name or code,
        "asset_class": asset_class,
        "source": source,
        "market": "crypto",
        "known": bool(extra.pop("known", False)),
    }
    out.update(extra)
    return out


def _lookup_static(code: str) -> dict | None:
    """默认池常量里的展示名 / 元数据。"""
    for idx in US_INDICES:
        if idx["symbol"] == code:
            return _with_gate(_ident(code, idx["name"], "us_index", "sina",
                          sina_symbol=idx.get("sina_symbol") or code,
                          znb=idx.get("znb"), known=True))
    for etf in US_ETF_DEFAULT:
        if etf["symbol"] == code:
            return _with_gate(_ident(code, etf["name"], "us_index", "gate", known=True))
    for idx in JP_INDICES:
        if idx["symbol"] == code:
            return _with_gate(_ident(code, idx["name"], "jp_index", "sina",
                          sina_gi=idx.get("sina_gi") or "NKY", known=True))
    for idx in KR_INDICES_KNOWN:
        if idx["symbol"] == code:
            return _with_gate(_ident(code, idx["name"], "kr_index", "naver",
                          naver_code=idx.get("naver_code") or code, known=True))
    for stk in JP_STOCKS_KNOWN:
        if stk["symbol"].upper() == code.upper():
            return _with_gate(_ident(stk["symbol"], stk["name"], "jp_stock", "naver",
                          naver_code=stk["symbol"], known=True))
    for stk in KR_STOCKS_KNOWN:
        if stk["symbol"] == code:
            return _with_gate(_ident(code, stk["name"], "kr_stock", "naver", known=True))
    if code in US_STOCKS:
        return _with_gate(_ident(code, code, "us_stock", "sina", known=True))
    extra = GATE_EXTRA_EQUITY.get(code)
    if extra:
        contract, name, cls = extra
        return _with_gate(_ident(code, name, cls, "gate", known=True, gate_contract=contract))
    if code in GATE_EQUITY_MAP:
        cls = mapped_asset_class(code)
        kwargs = {"known": True}
        if cls == "jp_stock":
            kwargs["naver_code"] = code
        elif cls == "kr_stock":
            kwargs["naver_code"] = code
        elif cls == "us_index":
            kwargs["sina_symbol"] = code
        return _with_gate(_ident(code, catalog_display_name(code), cls, "gate", **kwargs))
    if code in GOLD_CRYPTO_SYMBOLS:
        return _ident(code, GOLD_DISPLAY_NAME, "gold", "crypto", known=True)
    return None


def resolve_symbol(raw: str) -> dict | None:
    """
    把用户输入规范为内部 ident。
    例：AAPL、AAPL_USDT、7203.T、TM（丰田ADR代币）、005930、SAMSUNG、.INX、SPX500、PLTR、日经225指数、BTCUSDT、黄金。
    无法归类则返回 None（校验层报「未知标的」）。
    """
    s = (raw or "").strip()
    if not s:
        return None
    alias_key = s if s in _INDEX_ALIAS else s.upper().replace(" ", "")
    compact = _norm_crypto(s) if not s.startswith(".") else s.upper()
    if s in _INDEX_ALIAS:
        s = _INDEX_ALIAS[s]
    elif alias_key in _INDEX_ALIAS:
        s = _INDEX_ALIAS[alias_key]
    elif s in _GATE_ALIASES:
        s = _GATE_ALIASES[s]
    elif alias_key in _GATE_ALIASES:
        s = _GATE_ALIASES[alias_key]
    elif compact in _GATE_ALIASES:
        s = _GATE_ALIASES[compact]
    else:
        s = s.strip()

    hit = _lookup_static(s) or _lookup_static(s.upper())
    if hit:
        return hit

    cu = _norm_crypto(s)
    if cu in GOLD_CRYPTO_SYMBOLS:
        return _ident(cu, GOLD_DISPLAY_NAME, "gold", "crypto", known=True)
    # 股票代币必须在「当加密永续」之前识别，避免 AAPLUSDT 被收成币
    if is_crypto_perp_symbol(s) and cu not in GOLD_CRYPTO_SYMBOLS:
        return _ident(cu, cu, "crypto", "crypto")

    up = s.upper()
    if re.fullmatch(r"\d{3,5}\.T", up):
        digits = up[:-2]
        return _with_gate(_ident(f"{digits}.T", f"{digits}.T", "jp_stock", "naver",
                      naver_code=f"{digits}.T"))
    if re.fullmatch(r"\d{4}", s):
        return _with_gate(_ident(f"{s}.T", f"{s}.T", "jp_stock", "naver", naver_code=f"{s}.T"))
    if re.fullmatch(r"\d{6}", s):
        return _with_gate(_ident(s, s, "kr_stock", "naver"))

    if up in (".INX", ".DJI", ".IXIC", ".NDX"):
        names = {".INX": "标普500", ".DJI": "道指", ".IXIC": "纳指", ".NDX": "纳斯达克100"}
        znb = {".INX": "SPX", ".DJI": "DJI", ".IXIC": "IXIC", ".NDX": "NDX"}
        return _with_gate(_ident(up, names[up], "us_index", "sina", sina_symbol=up, znb=znb[up], known=True))

    # 美股 ticker：1–5 字母，可选 -B / .B
    us = up.replace(".", "-")
    if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", us):
        code = us
        return _with_gate(_ident(code, code, "us_stock", "sina"))
    return None


def override_fingerprint(symbols: list[str] | None = None) -> str:
    """扫描缓存身份：覆盖列表规范化后排序拼接。空 = 默认池。"""
    codes = symbols if symbols is not None else load_override_symbols()
    norm = []
    for s in codes or []:
        ident = resolve_symbol(s)
        if ident:
            norm.append(ident["code"])
    return ",".join(sorted(set(norm)))


def load_override_symbols() -> list[str]:
    """读取覆盖池。损坏/缺失视为空（走默认混合池）。"""
    try:
        if not OVERRIDE_FILE.is_file():
            return []
        data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        raw = data.get("symbols") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        out, seen = [], set()
        for x in raw:
            s = str(x or "").strip()
            if not s or s.upper() in seen:
                continue
            seen.add(s.upper())
            out.append(s)
        return out
    except Exception:
        return []


def save_override_symbols(symbols: list[str]) -> list[str]:
    """写入覆盖池 JSON。空列表 = 清除，扫描回默认池。"""
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cleaned, seen = [], set()
    for x in symbols or []:
        s = str(x or "").strip()
        if not s:
            continue
        ident = resolve_symbol(s)
        code = ident["code"] if ident else s
        if code.upper() in seen:
            continue
        seen.add(code.upper())
        cleaned.append(code)
    OVERRIDE_FILE.write_text(
        json.dumps({"symbols": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cleaned


def clear_override_symbols() -> None:
    save_override_symbols([])


def validate_symbol(code: str, crypto_ok=None, gate_ok=None) -> dict:
    """
    校验单只标的。返回 {ok, code, name, asset_class, source, reason}。
    crypto_ok(canonical) 用于永续是否在交易所 ticker 列表；黄金常量直接通过。
    gate_ok(contract) 用于 Gate 股票代币合约是否存在；有静态映射时可免于探活。
    """
    raw = (code or "").strip()
    ident = resolve_symbol(raw)
    if not ident:
        return {"ok": False, "code": raw, "name": raw, "asset_class": "",
                "source": "", "reason": "未知标的"}
    cls = ident["asset_class"]
    keys = ("code", "name", "asset_class", "source")
    extra = {}
    if ident.get("gate_contract"):
        extra["gate_contract"] = ident["gate_contract"]
    if ident.get("gate_venue"):
        extra["gate_venue"] = ident["gate_venue"]
    if ident.get("tokenized"):
        extra["tokenized"] = True
    if ident.get("token_role"):
        extra["token_role"] = ident["token_role"]
    if cls == "gold":
        return {"ok": True, "reason": "", **{k: ident[k] for k in keys}, **extra}
    if cls == "crypto":
        ok = True
        reason = ""
        if crypto_ok is not None:
            try:
                ok = bool(crypto_ok(ident["code"]))
            except Exception:
                ok = False
            if not ok:
                reason = "永续合约列表中不存在"
        return {"ok": ok, "reason": reason, **{k: ident[k] for k in keys}, **extra}

    contract = ident.get("gate_contract") or gate_contract_for(ident["code"])
    mapped = bool(gate_contract_for(ident["code"]))
    if mapped and contract:
        role = "proxy" if cls in ("jp_stock", "kr_stock", "jp_index", "kr_index") else None
        out = {"ok": True, "reason": "", "code": ident["code"], "name": ident["name"],
               "asset_class": cls, "source": "gate", "tokenized": True,
               "gate_contract": contract, "gate_venue": gate_venue_for_pair(contract)}
        if role:
            out["token_role"] = role
        return out
    if not contract and cls == "us_stock":
        contract = guess_us_gate_contract(ident["code"])
    if contract and gate_ok is not None:
        try:
            if gate_ok(contract):
                return {"ok": True, "reason": "", "code": ident["code"], "name": ident["name"],
                        "asset_class": cls, "source": "gate", "tokenized": True,
                        "gate_contract": contract,
                        "gate_venue": gate_venue_for_pair(contract)}
        except Exception:
            pass

    try:
        from equity_sources import validate_equity
        ok, reason = validate_equity(ident)
    except Exception as e:
        ok, reason = (True, "") if ident.get("known") else (False, str(e)[:80])
    return {"ok": ok, "reason": reason, **{k: ident[k] for k in keys}, **extra}


def validate_symbols(symbols: list[str], crypto_ok=None, gate_ok=None) -> dict:
    """批量校验。ok[] 为通过项，bad[] 为 {code, reason}。"""
    ok_rows, bad_rows, seen = [], [], set()
    for raw in symbols or []:
        s = str(raw or "").strip()
        if not s:
            continue
        key = s.upper()
        if key in seen:
            continue
        seen.add(key)
        row = validate_symbol(s, crypto_ok=crypto_ok, gate_ok=gate_ok)
        if row.get("ok"):
            payload = {k: row[k] for k in ("code", "name", "asset_class", "source") if k in row}
            if row.get("gate_contract"):
                payload["gate_contract"] = row["gate_contract"]
            if row.get("gate_venue"):
                payload["gate_venue"] = row["gate_venue"]
            if row.get("tokenized"):
                payload["tokenized"] = True
            if row.get("token_role"):
                payload["token_role"] = row["token_role"]
            ok_rows.append(payload)
        else:
            bad_rows.append({"code": s, "reason": row.get("reason") or "无法解析"})
    return {"ok": ok_rows, "bad": bad_rows}


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
    item = {
        "code": code,
        "name": name or code,
        "price": price,
        "chg": chg,
        "asset_class": asset_class,
        "market": "crypto",  # 仍走全市场标的 Tab / /api/crypto / kline market=crypto
        "source": source,
    }
    g = gate_contract_for(code)
    if g and asset_class not in ("crypto", "gold"):
        item["gate_contract"] = g
        item["tokenized"] = True
        item["source"] = "gate"
        item["gate_venue"] = gate_venue_for_pair(g)
        if asset_class in ("jp_stock", "kr_stock", "jp_index", "kr_index"):
            item["token_role"] = "proxy"
    return item


def _ident_to_pool_item(ident: dict, ticker_map: dict | None = None) -> dict:
    code = ident["code"]
    item = _pool_item(code, ident.get("name") or code,
                      ident.get("asset_class") or "us_stock",
                      ident.get("source") or "equity")
    tmap = ticker_map or {}
    t = tmap.get(_norm_crypto(code)) or tmap.get(code)
    if t:
        item["price"] = t.get("price")
        item["chg"] = t.get("chg")
    for k in ("sina_symbol", "naver_code", "znb", "sina_gi", "gate_contract", "gate_venue", "token_role"):
        if ident.get(k):
            item[k] = ident[k]
    if ident.get("tokenized"):
        item["tokenized"] = True
    return item


def build_override_pool(symbols: list[str], crypto_tickers: list[dict] | None = None,
                        gold: dict | None = None) -> list[dict]:
    """覆盖池非空：只扫这些标的（仍应用 crypto_interval / 形态）。无法 resolve 的跳过。"""
    ticker_map = {}
    for t in crypto_tickers or []:
        sym = _norm_crypto(str(t.get("symbol") or t.get("code") or ""))
        if sym:
            ticker_map[sym] = t
    pool, seen = [], set()
    for raw in symbols or []:
        ident = resolve_symbol(str(raw))
        if not ident:
            continue
        code = ident["code"]
        if code in seen:
            continue
        seen.add(code)
        if ident["asset_class"] == "gold" and gold and gold.get("code"):
            g = dict(gold)
            g.setdefault("name", GOLD_DISPLAY_NAME)
            g.setdefault("asset_class", "gold")
            g.setdefault("market", "crypto")
            g["code"] = gold.get("code") or code
            pool.append(g)
            continue
        pool.append(_ident_to_pool_item(ident, ticker_map))
    return pool


def build_global_pool(crypto_tickers: list[dict], top_n: int = CRYPTO_TOP_N,
                      gold: dict | None = None,
                      override_symbols: list[str] | None = None) -> list[dict]:
    """
    组装全市场标的 Tab 扫描池。

    override_symbols 非空：只使用这些标的。
    否则：涨幅 TOP N 永续 + 黄金 XAUT + 美股20/美指ETF（Gate 永续）+ 索尼/三星代理 + 日经/KOSPI 日K。
    黄金候选不占用 TOP N 名额。
    """
    if override_symbols:
        return build_override_pool(override_symbols, crypto_tickers=crypto_tickers, gold=gold)

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
        sym = _norm_crypto(str(t.get("symbol") or t.get("code") or ""))
        if not sym or sym in gold_codes or is_stock_token_ticker(sym):
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

    for idx in US_INDEX_DEFAULT:
        it = _pool_item(idx["symbol"], idx["name"], "us_index", "sina")
        it["sina_symbol"] = idx.get("sina_symbol") or idx["symbol"]
        if idx.get("znb"):
            it["znb"] = idx["znb"]
        add(it)
    for etf in US_ETF_DEFAULT:
        add(_pool_item(etf["symbol"], etf["name"], "us_index", "gate"))

    for stk in US_STOCKS:
        add(_pool_item(stk, stk, "us_stock", "sina"))

    for idx in JP_INDICES:
        add(_pool_item(idx["symbol"], idx["name"], "jp_index", "sina"))
    for stk in JP_STOCKS:
        it = _pool_item(stk["symbol"], stk["name"], "jp_stock", "naver")
        it["naver_code"] = stk["symbol"]
        add(it)
    for idx in KR_INDICES:
        it = _pool_item(idx["symbol"], idx["name"], "kr_index", "naver")
        it["naver_code"] = idx.get("naver_code") or idx["symbol"]
        add(it)
    for stk in KR_STOCKS:
        add(_pool_item(stk["symbol"], stk["name"], "kr_stock", "naver"))

    return pool


def static_global_counts() -> dict:
    """文档/测试用：静态侧数量（不含动态 TOP 币）。"""
    return {
        "gold": 1,
        "us_index": len(US_INDEX_DEFAULT) + len(US_ETF_DEFAULT),
        "us_stock": len(US_STOCKS),
        "jp_index": len(JP_INDICES),
        "jp_stock": len(JP_STOCKS),
        "kr_index": len(KR_INDICES),
        "kr_stock": len(KR_STOCKS),
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
    for host in YAHOO_CHART_HOSTS:
        url = (
            f"{host}/v8/finance/chart/{enc}"
            f"?interval={y_interval}&range={y_range}&includePrePost=false"
        )
        for attempt in range(3):
            try:
                r = _YAHOO_HTTP.get(url, timeout=YAHOO_TIMEOUT)
                if r.status_code == 429:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    break
                data = r.json()
                node = (data.get("chart") or {})
                if node.get("error"):
                    break
                result = node.get("result") or []
                if result:
                    return result[0]
                break
            except Exception:
                break
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
