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


class ResolveGoldTest(unittest.TestCase):
    def test_prefers_ticker_xau(self):
        tickers = [{"symbol": "XAUUSDT", "price": 2410.0, "chg": 0.3}]
        g = sc.resolve_gold_instrument(tickers)
        self.assertEqual(g["code"], "XAUUSDT")
        self.assertEqual(g["source"], "crypto")
        self.assertEqual(g["name"], "黄金")

    def test_yahoo_fallback_when_crypto_empty(self):
        def boom(*_a, **_k):
            raise TimeoutError("no perp gold")

        def fake_yahoo(symbol, interval="1d", lookback=8):
            if symbol != "GC=F":
                return None
            return {"code": "GC=F", "name": "Gold", "price": 2300.0, "chg": 0.1,
                    "bars": _daily_bars(8), "source": "yahoo"}

        with patch.object(sc, "fetch_crypto_kline", side_effect=boom), \
             patch.object(sc, "fetch_yahoo_instrument", side_effect=fake_yahoo):
            g = sc.resolve_gold_instrument([])
        self.assertEqual(g["code"], "GC=F")
        self.assertEqual(g["source"], "yahoo")
        self.assertEqual(g["name"], "黄金")


class RunCryptoScanPoolTest(unittest.TestCase):
    def test_scan_uses_mixed_pool_and_writes_interval(self):
        tickers = _tickers(22)
        bars = _daily_bars(50)
        events = []

        def fake_yahoo(symbol, interval="1d", lookback=200):
            return {
                "code": symbol, "name": symbol, "price": 12.0, "chg": 1.2,
                "bars": bars, "source": "yahoo",
            }

        with tempfile.TemporaryDirectory() as td:
            crypto_file = Path(td) / "crypto.json"
            with patch.object(sc, "fetch_crypto_tickers", return_value=tickers), \
                 patch.object(sc, "resolve_gold_instrument", return_value=_gold_row()), \
                 patch.object(sc, "fetch_crypto_kline", return_value=bars), \
                 patch.object(sc, "fetch_yahoo_instrument", side_effect=fake_yahoo), \
                 patch.object(sc, "load_crypto_interval", return_value="1d"), \
                 patch.object(sc, "CRYPTO_FILE", crypto_file), \
                 patch.object(sc, "DATA", Path(td)):
                rows = sc.run_crypto_scan(top=20, workers=4,
                                          progress=lambda m, **k: events.append(m))
            payload = json.loads(crypto_file.read_text(encoding="utf-8"))
        classes = {r["asset_class"] for r in rows}
        self.assertIn("crypto", classes)
        self.assertIn("gold", classes)
        self.assertIn("us_index", classes)
        self.assertIn("us_stock", classes)
        self.assertEqual(sum(1 for r in rows if r["asset_class"] == "crypto"), 20)
        self.assertEqual(sum(1 for r in rows if r["asset_class"] == "us_stock"), 20)
        self.assertEqual(payload["crypto_interval"], "1d")
        self.assertEqual(payload["scope"], "crypto")
        self.assertEqual(payload["gold"]["code"], "XAUUSDT")
        self.assertTrue(any("全球池" in m or "TOP" in m for m in events))


class IntervalCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.crypto = Path(self.tmp.name) / "crypto.json"
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.p_c = patch.object(sc, "CRYPTO_FILE", self.crypto)
        self.p_w = patch.object(server, "WATCH_FILE", self.watch)
        self.p_c.start()
        self.p_w.start()
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        self.p_c.stop()
        self.p_w.stop()
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
        self.assertIn("全球池", self.html)
        self.assertIn("poolFilterWrap", self.html)
        self.assertIn("仅美股", self.html)
        self.assertIn("黄金+指数", self.html)
        self.assertIn("asset-badge", self.html)


if __name__ == "__main__":
    unittest.main()
