#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全球池、K 线周期规范化、1h 重采样、扫描缓存身份含 interval。不跑全市场扫描。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import global_pool as gp  # noqa: E402
import scanner as sc  # noqa: E402
import server  # noqa: E402

BJT = timezone(timedelta(hours=8))
DASH = ROOT / "dashboard.html"


def _h1_bars(n: int = 8, start: str = "2024-01-02 00:00") -> list[dict]:
    dt = datetime.strptime(start, "%Y-%m-%d %H:%M")
    out = []
    for i in range(n):
        t = dt + timedelta(hours=i)
        out.append({
            "date": t.strftime("%Y-%m-%d %H:%M"),
            "open": 100.0 + i,
            "high": 101.0 + i,
            "low": 99.0 + i,
            "close": 100.5 + i,
            "vol": 10.0 + i,
        })
    return out


def _daily_bars(n: int = 50) -> list[dict]:
    d0 = datetime(2024, 1, 2)
    out = []
    for i in range(n):
        out.append({
            "date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
            "open": 10.0 + i * 0.01,
            "close": 10.1 + i * 0.01,
            "high": 10.3 + i * 0.01,
            "low": 9.9 + i * 0.01,
            "vol": 1000 + i,
        })
    return out


def _tickers(n: int = 25) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append({"symbol": f"C{i:02d}USDT", "price": 1.0 + i, "chg": 30.0 - i, "quote_volume": 1})
    return rows


def _gold_row() -> dict:
    return {
        "code": "XAUUSDT", "name": "黄金", "price": 2400.0, "chg": 0.4,
        "asset_class": "gold", "market": "crypto", "source": "crypto",
    }


class NormalizeIntervalTest(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(sc.normalize_crypto_interval("4h"), "4h")
        self.assertEqual(sc.normalize_crypto_interval("8H"), "8h")
        self.assertEqual(sc.normalize_crypto_interval("1d"), "1d")
        self.assertEqual(sc.normalize_crypto_interval("1日"), "1d")
        self.assertEqual(sc.normalize_crypto_interval("daily"), "1d")

    def test_default_on_empty_or_junk(self):
        self.assertEqual(sc.normalize_crypto_interval(None), "1d")
        self.assertEqual(sc.normalize_crypto_interval(""), "1d")
        self.assertEqual(sc.normalize_crypto_interval("15m"), "1d")
        self.assertEqual(sc.DEFAULT_CRYPTO_INTERVAL, "1d")
        self.assertEqual(sc.CRYPTO_INTERVALS, ("4h", "8h", "1d"))

    def test_load_from_config_json(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"crypto_interval": "4h"}), encoding="utf-8")
            with patch.object(sc, "DATA", data):
                self.assertEqual(sc.load_crypto_interval(), "4h")
            (data / "config.json").write_text("{}", encoding="utf-8")
            with patch.object(sc, "DATA", data):
                self.assertEqual(sc.load_crypto_interval(), "1d")


class ResampleTest(unittest.TestCase):
    def test_1h_to_4h(self):
        bars = _h1_bars(8)
        out = gp.resample_ohlc_hours(bars, 4)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], "2024-01-02 00:00")
        self.assertEqual(out[0]["open"], 100.0)
        self.assertEqual(out[0]["close"], 103.5)  # hour 3 close
        self.assertEqual(out[0]["high"], 104.0)   # hour 3 high 101+3
        self.assertEqual(out[0]["low"], 99.0)
        self.assertEqual(out[0]["vol"], 10 + 11 + 12 + 13)
        self.assertEqual(out[1]["date"], "2024-01-02 04:00")
        self.assertEqual(out[1]["open"], 104.0)

    def test_1h_to_8h(self):
        bars = _h1_bars(8)
        out = gp.resample_ohlc_hours(bars, 8)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["date"], "2024-01-02 00:00")
        self.assertEqual(out[0]["open"], 100.0)
        self.assertEqual(out[0]["close"], 107.5)
        self.assertEqual(out[0]["vol"], sum(10.0 + i for i in range(8)))

    def test_incomplete_last_bucket_kept(self):
        bars = _h1_bars(5)  # 00-04 → 4h 完整一桶 + 04 单独一桶
        out = gp.resample_ohlc_hours(bars, 4)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["open"], 104.0)
        self.assertEqual(out[1]["close"], 104.5)

    def test_passthrough_on_bad_hours(self):
        bars = _h1_bars(3)
        self.assertEqual(gp.resample_ohlc_hours(bars, 3), bars)
        self.assertEqual(gp.resample_ohlc_hours([], 4), [])


class PoolBuilderTest(unittest.TestCase):
    def test_mixed_pool_shape(self):
        tickers = _tickers(25)
        gold = _gold_row()
        pool = gp.build_global_pool(tickers, top_n=20, gold=gold)
        by = {}
        for it in pool:
            by.setdefault(it["asset_class"], []).append(it)
        self.assertEqual(len(by["crypto"]), 20)
        self.assertEqual(by["crypto"][0]["code"], "C00USDT")
        self.assertEqual(len(by["gold"]), 1)
        self.assertEqual(by["gold"][0]["code"], "XAUUSDT")
        self.assertEqual(by["gold"][0]["name"], "黄金")
        self.assertEqual(len(by["us_index"]), len(gp.US_INDICES))
        self.assertEqual({x["code"] for x in by["us_index"]}, {x["symbol"] for x in gp.US_INDICES})
        self.assertEqual(len(by["us_stock"]), 20)
        self.assertEqual({x["code"] for x in by["us_stock"]}, set(gp.US_STOCKS))
        self.assertTrue(all(x["market"] == "crypto" for x in pool))
        self.assertEqual(sc.CRYPTO_TOP_N, 20)
        self.assertIn("jp_index", by)
        self.assertEqual({x["code"] for x in by["jp_index"]}, {x["symbol"] for x in gp.JP_INDICES})
        self.assertIn("NKY", {x["code"] for x in by["jp_index"]})
        self.assertIn("KOSPI", {x["code"] for x in by["kr_index"]})
        self.assertIn("KOSDAQ", {x["code"] for x in by["kr_index"]})
        self.assertIn("7203.T", {x["code"] for x in by["jp_stock"]})
        self.assertIn("005930", {x["code"] for x in by["kr_stock"]})
        self.assertTrue(all(x["source"] != "yahoo" for x in pool if x["asset_class"] != "crypto"))
        self.assertEqual(gp.gate_contract_for("AAPL"), "AAPL_USDT")
        self.assertEqual(gp.gate_contract_for(".INX"), "SPX500_USDT")
        self.assertEqual(gp.gate_contract_for(".DJI"), "US30_USDT")
        self.assertEqual(gp.gate_contract_for(".NDX"), "NAS100_USDT")
        self.assertIsNone(gp.gate_contract_for(".IXIC"))
        self.assertIsNone(gp.gate_contract_for("MA"))
        self.assertIsNone(gp.gate_contract_for("NKY"))
        self.assertIsNone(gp.gate_contract_for("KOSPI"))
        aapl = next(x for x in by["us_stock"] if x["code"] == "AAPL")
        self.assertTrue(aapl.get("tokenized"))
        self.assertEqual(aapl["source"], "gate")
        self.assertEqual(aapl["gate_contract"], "AAPL_USDT")
        ma = next(x for x in by["us_stock"] if x["code"] == "MA")
        self.assertFalse(ma.get("tokenized"))
        self.assertEqual(ma["source"], "sina")
        nky = next(x for x in by["jp_index"] if x["code"] == "NKY")
        self.assertEqual(nky["source"], "sina")
        toyota = next(x for x in by["jp_stock"] if x["code"] == "7203.T")
        self.assertFalse(toyota.get("tokenized"))
        self.assertIsNone(gp.gate_contract_for("7203.T"))
        self.assertEqual(next(x for x in by["jp_stock"] if x["code"] == "6758.T")["gate_contract"], "SONY_USDT")
        self.assertEqual(next(x for x in by["kr_stock"] if x["code"] == "005930")["gate_contract"], "SAMSUNG_USDT")

    def test_gold_perp_not_counted_in_topn(self):
        tickers = [{"symbol": "XAUUSDT", "price": 1, "chg": 99}] + _tickers(20)
        pool = gp.build_global_pool(tickers, top_n=20, gold=_gold_row())
        crypto = [x for x in pool if x["asset_class"] == "crypto"]
        golds = [x for x in pool if x["asset_class"] == "gold"]
        self.assertEqual(len(crypto), 20)
        self.assertEqual(len(golds), 1)
        self.assertFalse(any(x["code"] == "XAUUSDT" for x in crypto))

    def test_no_gold_omits_row(self):
        pool = gp.build_global_pool(_tickers(5), top_n=5, gold=None)
        self.assertFalse(any(x["asset_class"] == "gold" for x in pool))
        self.assertEqual(sum(1 for x in pool if x["asset_class"] == "crypto"), 5)

    def test_yahoo_symbol_detection(self):
        self.assertTrue(gp.is_yahoo_symbol("AAPL"))
        self.assertTrue(gp.is_yahoo_symbol("^GSPC"))
        self.assertTrue(gp.is_yahoo_symbol("GC=F"))
        self.assertTrue(gp.is_yahoo_symbol("BRK-B"))
        self.assertFalse(gp.is_yahoo_symbol("BTCUSDT"))
        self.assertFalse(gp.is_yahoo_symbol("XAUUSDT"))

    def test_override_empty_uses_default(self):
        pool = gp.build_global_pool(_tickers(5), top_n=5, gold=_gold_row(), override_symbols=None)
        self.assertGreater(len(pool), 5)
        self.assertTrue(any(x["asset_class"] == "crypto" for x in pool))
        self.assertTrue(any(x["code"] == "NKY" for x in pool))

    def test_override_nonempty_only_those_symbols(self):
        pool = gp.build_global_pool(
            _tickers(25), top_n=20, gold=_gold_row(),
            override_symbols=["AAPL", "7203.T", "BTCUSDT"],
        )
        codes = [x["code"] for x in pool]
        self.assertEqual(set(codes), {"AAPL", "7203.T", "BTCUSDT"})
        self.assertFalse(any(x["asset_class"] == "us_index" for x in pool))
        by = {x["code"]: x for x in pool}
        self.assertEqual(by["AAPL"]["asset_class"], "us_stock")
        self.assertEqual(by["7203.T"]["asset_class"], "jp_stock")
        self.assertEqual(by["BTCUSDT"]["asset_class"], "crypto")

    def test_resolve_symbol_aliases(self):
        self.assertEqual(gp.resolve_symbol("AAPL")["asset_class"], "us_stock")
        self.assertEqual(gp.resolve_symbol("^GSPC")["code"], ".INX")
        self.assertEqual(gp.resolve_symbol("标普500")["code"], ".INX")
        self.assertEqual(gp.resolve_symbol("日经225指数")["code"], "NKY")
        self.assertEqual(gp.resolve_symbol("7203.T")["asset_class"], "jp_stock")
        self.assertEqual(gp.resolve_symbol("005930")["asset_class"], "kr_stock")
        self.assertEqual(gp.resolve_symbol("黄金")["code"], "XAUTUSDT")
        self.assertEqual(gp.resolve_symbol("BTCUSDT")["asset_class"], "crypto")
        self.assertEqual(gp.resolve_symbol("AAPL_USDT")["code"], "AAPL")
        self.assertEqual(gp.resolve_symbol("AAPLUSDT")["code"], "AAPL")
        self.assertTrue(gp.resolve_symbol("AAPL")["tokenized"])
        self.assertEqual(gp.resolve_symbol("AAPL")["gate_contract"], "AAPL_USDT")
        self.assertEqual(gp.resolve_symbol("TM")["code"], "TM")
        self.assertEqual(gp.resolve_symbol("TM")["name"], "丰田ADR")
        self.assertEqual(gp.resolve_symbol("TM")["gate_contract"], "TM_USDT")
        self.assertFalse(gp.resolve_symbol("7203.T").get("tokenized"))
        self.assertEqual(gp.resolve_symbol("SAMSUNG")["code"], "005930")
        self.assertEqual(gp.resolve_symbol("SPX500")["code"], ".INX")
        self.assertEqual(gp.resolve_symbol("NAS100")["code"], ".NDX")
        self.assertEqual(gp.resolve_symbol("QQQ")["gate_contract"], "QQQ_USDT")
        self.assertEqual(gp.resolve_symbol("PLTR")["gate_contract"], "PLTR_USDT")
        self.assertEqual(gp.resolve_symbol("PLTR_USDT")["code"], "PLTR")
        self.assertIsNone(gp.guess_us_gate_contract("DIA"))
        self.assertFalse(gp.resolve_symbol("DIA").get("tokenized"))
        self.assertIsNone(gp.resolve_symbol("NOT_A_THING_ZZZ"))
        self.assertIsNone(gp.resolve_symbol(""))


class ResolveGoldTest(unittest.TestCase):
    def test_prefers_xaut_over_xau(self):
        tickers = [
            {"symbol": "XAUUSDT", "price": 2410.0, "chg": 0.3},
            {"symbol": "XAUTUSDT", "price": 2420.0, "chg": 0.5},
        ]
        g = sc.resolve_gold_instrument(tickers)
        self.assertEqual(g["code"], "XAUTUSDT")
        self.assertEqual(g["source"], "crypto")
        self.assertEqual(g["name"], "黄金")

    def test_falls_back_to_xau_when_xaut_missing(self):
        tickers = [{"symbol": "XAUUSDT", "price": 2410.0, "chg": 0.3}]
        g = sc.resolve_gold_instrument(tickers)
        self.assertEqual(g["code"], "XAUUSDT")

    def test_kline_fallback_when_ticker_empty(self):
        def fake_kline(sym, limit=8, interval="1d"):
            if sym != "XAUTUSDT":
                return []
            return _daily_bars(8)

        with patch.object(sc, "fetch_crypto_kline", side_effect=fake_kline):
            g = sc.resolve_gold_instrument([])
        self.assertEqual(g["code"], "XAUTUSDT")
        self.assertEqual(g["source"], "crypto")
        self.assertEqual(g["name"], "黄金")


class RunCryptoScanPoolTest(unittest.TestCase):
    def test_scan_uses_mixed_pool_and_writes_interval(self):
        tickers = _tickers(22)
        bars = _daily_bars(50)
        events = []

        def fake_inst(code, interval="1d", lookback=200, hint_source=None, hint_class=None):
            return {
                "code": code, "name": code, "price": 12.0, "chg": 1.2,
                "bars": bars, "source": hint_source or "sina",
                "asset_class": hint_class or "us_stock",
            }

        with tempfile.TemporaryDirectory() as td:
            crypto_file = Path(td) / "crypto.json"
            ov_file = Path(td) / "global_override_pool.json"
            with patch.object(sc, "fetch_crypto_tickers", return_value=tickers), \
                 patch.object(sc, "resolve_gold_instrument", return_value=_gold_row()), \
                 patch.object(sc, "fetch_global_instrument", side_effect=fake_inst), \
                 patch.object(sc, "load_crypto_interval", return_value="1d"), \
                 patch.object(sc, "CRYPTO_FILE", crypto_file), \
                 patch.object(sc, "DATA", Path(td)), \
                 patch.object(gp, "OVERRIDE_FILE", ov_file), \
                 patch.object(sc, "load_override_symbols", return_value=[]):
                rows = sc.run_crypto_scan(top=20, workers=4,
                                          progress=lambda m, **k: events.append(m))
            payload = json.loads(crypto_file.read_text(encoding="utf-8"))
        classes = {r["asset_class"] for r in rows}
        self.assertIn("crypto", classes)
        self.assertIn("gold", classes)
        self.assertIn("us_index", classes)
        self.assertIn("us_stock", classes)
        self.assertIn("jp_index", classes)
        self.assertIn("kr_index", classes)
        self.assertEqual(sum(1 for r in rows if r["asset_class"] == "crypto"), 20)
        self.assertEqual(sum(1 for r in rows if r["asset_class"] == "us_stock"), 20)
        self.assertEqual(payload["crypto_interval"], "1d")
        self.assertEqual(payload["scope"], "crypto")
        self.assertEqual(payload["pool_mode"], "default")
        self.assertEqual(payload["gold"]["code"], "XAUUSDT")
        self.assertTrue(any("全市场标的" in m or "默认" in m or "TOP" in m for m in events))

    def test_scan_override_only_requested_symbols(self):
        bars = _daily_bars(50)
        tickers = _tickers(22)

        def fake_inst(code, interval="1d", lookback=200, hint_source=None, hint_class=None):
            return {
                "code": code, "name": code, "price": 12.0, "chg": 1.2,
                "bars": bars, "source": "sina", "asset_class": hint_class or "us_stock",
            }

        with tempfile.TemporaryDirectory() as td:
            crypto_file = Path(td) / "crypto.json"
            with patch.object(sc, "fetch_crypto_tickers", return_value=tickers), \
                 patch.object(sc, "resolve_gold_instrument", return_value=None), \
                 patch.object(sc, "fetch_global_instrument", side_effect=fake_inst), \
                 patch.object(sc, "load_crypto_interval", return_value="1d"), \
                 patch.object(sc, "CRYPTO_FILE", crypto_file), \
                 patch.object(sc, "DATA", Path(td)), \
                 patch.object(sc, "load_override_symbols", return_value=["AAPL", "7203.T"]):
                rows = sc.run_crypto_scan(top=20, workers=2)
            payload = json.loads(crypto_file.read_text(encoding="utf-8"))
        self.assertEqual({r["code"] for r in rows}, {"AAPL", "7203.T"})
        self.assertEqual(payload["pool_mode"], "override")
        self.assertEqual(payload["override_symbols"], ["AAPL", "7203.T"])


class IntervalCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.crypto = Path(self.tmp.name) / "crypto.json"
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.ov = Path(self.tmp.name) / "global_override_pool.json"
        self.p_c = patch.object(sc, "CRYPTO_FILE", self.crypto)
        self.p_w = patch.object(server, "WATCH_FILE", self.watch)
        self.p_o = patch.object(gp, "OVERRIDE_FILE", self.ov)
        self.p_c.start()
        self.p_w.start()
        self.p_o.start()
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        self.p_c.stop()
        self.p_w.stop()
        self.p_o.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _crypto_payload(self, interval="1d"):
        now = datetime.now(BJT)
        rows = [{"code": "BTCUSDT", "score": 80, "qualified": False, "market": "crypto"}]
        p = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": now.isoformat(timespec="seconds"),
            "scope": "crypto",
            "done": True,
            "universe_size": 10,
            "candidates": rows,
            "items": rows,
            "box_mode": "classic",
            "pattern_family": "box",
            "crypto_interval": interval,
        }
        return p

    def test_hit_when_interval_matches(self):
        self.crypto.write_text(json.dumps(self._crypto_payload("1d")), encoding="utf-8")
        server.STATE["config"]["crypto_interval"] = "1d"
        hit = server.scan_cache_response("crypto", force=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["status"], "cached")

    def test_miss_when_interval_differs(self):
        self.crypto.write_text(json.dumps(self._crypto_payload("1d")), encoding="utf-8")
        server.STATE["config"]["crypto_interval"] = "4h"
        self.assertIsNone(server.scan_cache_response("crypto", force=False))

    def test_missing_interval_field_treated_as_1d(self):
        p = self._crypto_payload("1d")
        p.pop("crypto_interval")
        self.crypto.write_text(json.dumps(p), encoding="utf-8")
        server.STATE["config"]["crypto_interval"] = "1d"
        self.assertIsNotNone(server.scan_cache_response("crypto", force=False))
        server.STATE["config"]["crypto_interval"] = "8h"
        self.assertIsNone(server.scan_cache_response("crypto", force=False))

    def test_miss_when_override_fingerprint_differs(self):
        p = self._crypto_payload("1d")
        p["override_fingerprint"] = ""
        self.crypto.write_text(json.dumps(p), encoding="utf-8")
        server.STATE["config"]["crypto_interval"] = "1d"
        self.assertIsNotNone(server.scan_cache_response("crypto", force=False))
        gp.save_override_symbols(["AAPL"])
        self.assertIsNone(server.scan_cache_response("crypto", force=False))

    def test_market_cache_ignores_crypto_interval(self):
        now = datetime.now(BJT)
        rows = [{"code": "000001", "score": 90, "qualified": True}]
        self.watch.write_text(json.dumps({
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": now.isoformat(timespec="seconds"),
            "scope": "market",
            "done": True,
            "universe_size": 10,
            "candidates": rows,
            "items": rows,
            "box_mode": "classic",
            "pattern_family": "box",
        }), encoding="utf-8")
        server.STATE["config"]["crypto_interval"] = "4h"
        hit = server.scan_cache_response("market", force=False)
        self.assertIsNotNone(hit)


class ConfigIntervalHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.p = patch.object(server, "CONFIG_FILE", self.cfg_path)
        self.p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_get_post_crypto_interval(self):
        got = self._req("GET", "api/config")
        self.assertEqual(got["crypto_interval"], "1d")
        self.assertEqual(got["crypto_intervals"], ["4h", "8h", "1d"])
        saved = self._req("POST", "api/config", {"crypto_interval": "4h"})
        self.assertEqual(saved["config"]["crypto_interval"], "4h")
        disk = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["crypto_interval"], "4h")
        junk = self._req("POST", "api/config", {"crypto_interval": "15m"})
        self.assertEqual(junk["config"]["crypto_interval"], "1d")


class YahooRetryTest(unittest.TestCase):
    def test_429_then_200(self):
        calls = {"n": 0}
        result = {
            "timestamp": [1_700_000_000],
            "indicators": {"quote": [{
                "open": [10], "high": [11], "low": [9], "close": [10.5], "volume": [100],
            }]},
            "meta": {"shortName": "Apple Inc.", "regularMarketPrice": 10.5, "previousClose": 10},
        }

        class FakeResp:
            def __init__(self, code, payload=None):
                self.status_code = code
                self._payload = payload or {}

            def json(self):
                return self._payload

        def fake_get(url, timeout=10):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(429)
            return FakeResp(200, {"chart": {"result": [result]}})

        with patch.object(gp._YAHOO_HTTP, "get", side_effect=fake_get), \
             patch.object(gp.time, "sleep"):
            node = gp._yahoo_chart_json("AAPL", "1d", "5d")
        self.assertIsNotNone(node)
        self.assertEqual(calls["n"], 2)


class FormatBarDateTest(unittest.TestCase):
    def test_daily_vs_intraday(self):
        ts = datetime(2024, 3, 1, 16, 0).timestamp()
        self.assertEqual(sc.format_bar_date(ts, "1d"), datetime.fromtimestamp(ts).strftime("%Y-%m-%d"))
        self.assertIn("16:00", sc.format_bar_date(ts, "4h"))
        self.assertRegex(sc.format_bar_date(ts, "8h"), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


class DashboardGlobalPoolContractTest(unittest.TestCase):
    def setUp(self):
        self.html = DASH.read_text(encoding="utf-8")

    def test_interval_pills_and_filters(self):
        self.assertIn("cryptoIntervalWrap", self.html)
        self.assertIn('data-interval="4h"', self.html)
        self.assertIn('data-interval="8h"', self.html)
        self.assertIn('data-interval="1d"', self.html)
        self.assertIn("setCryptoInterval", self.html)
        self.assertIn("全市场标的", self.html)
        self.assertNotIn(">加密货币<", self.html)
        self.assertIn("poolFilterWrap", self.html)
        self.assertIn("仅美股", self.html)
        self.assertIn("日股", self.html)
        self.assertIn("韩股", self.html)
        self.assertIn("黄金+指数", self.html)
        self.assertIn("asset-badge", self.html)
        self.assertIn("hpCoverWrap", self.html)
        self.assertIn("coverModal", self.html)
        self.assertIn("openCoverModal", self.html)
        self.assertIn("api/global_pool/override", self.html)
        self.assertIn("覆盖池已变更", self.html)
        self.assertIn("股票代币", self.html)
        self.assertIn("美股代币", self.html)
        self.assertIn("新浪分钟K", self.html)


class ValidateSymbolTest(unittest.TestCase):
    def test_known_without_live(self):
        row = gp.validate_symbol("黄金", crypto_ok=lambda *_: False)
        self.assertTrue(row["ok"])
        self.assertEqual(row["code"], "XAUTUSDT")
        self.assertEqual(row["asset_class"], "gold")

    def test_crypto_callback(self):
        ok = gp.validate_symbol("BTCUSDT", crypto_ok=lambda c: c == "BTCUSDT")
        self.assertTrue(ok["ok"])
        bad = gp.validate_symbol("NOPEUSDT", crypto_ok=lambda c: False)
        self.assertFalse(bad["ok"])
        self.assertIn("永续", bad["reason"])

    def test_unknown(self):
        row = gp.validate_symbol("!!!", crypto_ok=lambda *_: True)
        self.assertFalse(row["ok"])
        self.assertEqual(row["reason"], "未知标的")

    def test_batch_reports_rejected(self):
        def fake_eq(ident):
            if ident["asset_class"] == "us_stock" and ident["code"] == "AAPL":
                return True, ""
            if ident.get("known"):
                return True, ""
            return False, "新浪/Naver 无此美股"

        with patch("equity_sources.validate_equity", side_effect=fake_eq):
            out = gp.validate_symbols(
                ["AAPL", "FOO999", "日经225指数", "BTCUSDT"],
                crypto_ok=lambda c: c == "BTCUSDT",
            )
        codes = {x["code"] for x in out["ok"]}
        self.assertIn("AAPL", codes)
        self.assertIn("NKY", codes)
        self.assertIn("BTCUSDT", codes)
        self.assertTrue(any(x["code"] == "FOO999" for x in out["bad"]))


class OverridePersistTest(unittest.TestCase):
    def test_save_load_clear(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ov.json"
            with patch.object(gp, "OVERRIDE_FILE", path):
                self.assertEqual(gp.load_override_symbols(), [])
                saved = gp.save_override_symbols(["AAPL", "7203.T", "AAPL"])
                self.assertEqual(saved, ["AAPL", "7203.T"])
                self.assertEqual(gp.load_override_symbols(), ["AAPL", "7203.T"])
                self.assertTrue(gp.override_fingerprint().startswith("7203.T") or "AAPL" in gp.override_fingerprint())
                gp.clear_override_symbols()
                self.assertEqual(gp.load_override_symbols(), [])
                self.assertEqual(gp.override_fingerprint(), "")


class OverrideHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ov = Path(self.tmp.name) / "ov.json"
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.p = patch.object(gp, "OVERRIDE_FILE", self.ov)
        self.p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_get_empty_default(self):
        got = self._req("GET", "api/global_pool/override")
        self.assertEqual(got["mode"], "default")
        self.assertEqual(got["count"], 0)

    def test_validate_and_save_only_valids(self):
        def fake_eq(ident):
            if ident["code"] in ("AAPL", "NKY") or ident.get("known"):
                return True, ""
            return False, "新浪/Naver 无此美股"

        with patch("equity_sources.validate_equity", side_effect=fake_eq), \
             patch.object(sc, "crypto_symbol_is_listed", return_value=True):
            v = self._req("POST", "api/global_pool/validate",
                          {"symbols": ["AAPL", "NOTREALZZ", "BTCUSDT"]})
            saved = self._req("POST", "api/global_pool/override",
                              {"symbols": ["AAPL", "NOTREALZZ", "BTCUSDT"]})
        self.assertTrue(any(x["code"] == "AAPL" for x in v["ok"]))
        self.assertTrue(any(x["code"] == "NOTREALZZ" for x in v["bad"]))
        self.assertIn("AAPL", saved["symbols"])
        self.assertIn("BTCUSDT", saved["symbols"])
        self.assertNotIn("NOTREALZZ", saved["symbols"])
        self.assertEqual(saved["mode"], "override")
        self.assertTrue(saved["rejected"])

    def test_clear(self):
        gp.save_override_symbols(["AAPL"])
        out = self._req("POST", "api/global_pool/override", {"action": "clear"})
        self.assertEqual(out["mode"], "default")
        self.assertEqual(out["symbols"], [])


class GateTokenPathTest(unittest.TestCase):
    def test_aapl_uses_gate_not_binance_and_keeps_4h(self):
        calls = []

        def fake_gate(contract, limit=200, interval="1d"):
            calls.append((contract, interval))
            return _daily_bars(50)

        with patch.object(sc, "fetch_gate_equity_klines", side_effect=fake_gate), \
             patch.object(sc, "fetch_crypto_kline",
                          side_effect=AssertionError("股票代币不得走 Binance 粘性路径")):
            inst = sc.fetch_global_instrument("AAPL", interval="4h")
        self.assertEqual(calls, [("AAPL_USDT", "4h")])
        self.assertTrue(inst["tokenized"])
        self.assertEqual(inst["source"], "gate")
        self.assertEqual(inst["gate_contract"], "AAPL_USDT")
        self.assertIsNone(inst.get("interval_note"))
        self.assertFalse(inst.get("interval_limited"))

    def test_alias_aapl_usdt_resolves_and_fetches_gate(self):
        with patch.object(sc, "fetch_gate_equity_klines",
                          return_value=_daily_bars(50)) as mock_g, \
             patch.object(sc, "fetch_crypto_kline",
                          side_effect=AssertionError("no binance")):
            inst = sc.fetch_global_instrument("AAPL_USDT", interval="1d")
        mock_g.assert_called_once()
        self.assertEqual(inst["code"], "AAPL")
        self.assertTrue(inst["tokenized"])

    def test_nky_has_no_gate_token_falls_back(self):
        inst = {
            "code": "NKY", "name": "日经225", "price": 1, "chg": 0,
            "bars": _daily_bars(50), "source": "sina", "asset_class": "jp_index",
            "interval_note": "股票/指数无稳定 4h/8h 历史，已用日K",
            "interval_limited": True, "tokenized": False,
        }
        with patch.object(sc, "fetch_gate_equity_klines",
                          side_effect=AssertionError("NKY 无代币不应打 Gate")), \
             patch("equity_sources.fetch_equity_instrument", return_value=inst):
            out = sc.fetch_global_instrument("NKY", interval="4h")
        self.assertTrue(out["interval_limited"])
        self.assertEqual(out["source"], "sina")

    def test_validate_gate_alias_without_crypto_list(self):
        row = gp.validate_symbol("AAPL_USDT", crypto_ok=lambda *_: False, gate_ok=lambda *_: False)
        self.assertTrue(row["ok"])
        self.assertEqual(row["code"], "AAPL")
        self.assertEqual(row["source"], "gate")
        self.assertEqual(row["gate_contract"], "AAPL_USDT")

    def test_gate_equity_excluded_from_crypto_topn(self):
        tickers = [{"symbol": "AAPLUSDT", "price": 1, "chg": 99}] + _tickers(20)
        pool = gp.build_global_pool(tickers, top_n=20, gold=_gold_row())
        crypto = [x for x in pool if x["asset_class"] == "crypto"]
        self.assertFalse(any(x["code"] == "AAPLUSDT" for x in crypto))
        self.assertEqual(len(crypto), 20)
        self.assertTrue(any(x["code"] == "AAPL" and x.get("tokenized") for x in pool))

    def test_xstock_and_leverage_excluded_from_crypto_topn(self):
        tickers = (
            [{"symbol": "AAPLXUSDT", "price": 1, "chg": 99}]
            + [{"symbol": "AAPL3LUSDT", "price": 1, "chg": 98}]
            + _tickers(20)
        )
        pool = gp.build_global_pool(tickers, top_n=20, gold=_gold_row())
        crypto = [x for x in pool if x["asset_class"] == "crypto"]
        self.assertFalse(any(x["code"] in ("AAPLXUSDT", "AAPL3LUSDT") for x in crypto))
        self.assertEqual(len(crypto), 20)

    def test_ma_unlisted_skips_gate_kline(self):
        mink = {
            "code": "MA", "name": "MA", "price": 550.0, "chg": 0,
            "bars": _h1_bars(8), "source": "sina", "asset_class": "us_stock",
            "interval": "4h", "interval_note": None, "interval_limited": False,
            "tokenized": False,
        }
        with patch.object(sc, "listed_gate_contracts", return_value={"AAPL_USDT"}), \
             patch.object(sc, "fetch_gate_equity_klines",
                          side_effect=AssertionError("MA 无合约不应打 Gate")), \
             patch("equity_sources.fetch_equity_instrument", return_value=mink):
            out = sc.fetch_global_instrument("MA", interval="4h")
        self.assertEqual(out["source"], "sina")
        self.assertFalse(out.get("tokenized"))
        self.assertIsNone(out.get("interval_note"))

    def test_pltr_extra_maps_to_gate(self):
        with patch.object(sc, "fetch_gate_equity_klines",
                          return_value=_daily_bars(50)) as mock_g, \
             patch.object(sc, "fetch_crypto_kline",
                          side_effect=AssertionError("no binance")):
            inst = sc.fetch_global_instrument("PLTR", interval="4h")
        mock_g.assert_called_once()
        self.assertEqual(mock_g.call_args[0][0], "PLTR_USDT")
        self.assertTrue(inst["tokenized"])
        row = gp.validate_symbol("PLTR_USDT", crypto_ok=lambda *_: False, gate_ok=lambda *_: False)
        self.assertTrue(row["ok"])
        self.assertEqual(row["gate_contract"], "PLTR_USDT")


if __name__ == "__main__":
    unittest.main()
