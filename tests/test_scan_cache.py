#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描结果 1 小时缓存：逻辑与 HTTP 冒烟（不触发全市场 universe 拉取）。"""
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

import scanner as sc  # noqa: E402
import server  # noqa: E402

BJT = timezone(timedelta(hours=8))


def _fresh_payload(**extra):
    now = datetime.now(BJT)
    rows = extra.pop("candidates", [{"code": "000001", "name": "测", "score": 90, "qualified": True}])
    payload = {
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": now.isoformat(timespec="seconds"),
        "scope": "market",
        "done": True,
        "universe_size": 5000,
        "candidates": rows,
        "items": rows,
        "hot_topics": [],
    }
    payload.update(extra)
    return payload


class CacheLogicTest(unittest.TestCase):
    def test_fresh_iso_updated(self):
        p = _fresh_payload()
        self.assertTrue(sc.is_fresh_scan_cache(p))
        self.assertLess(sc.cache_age_sec(p), 60)

    def test_stale_after_ttl(self):
        old = datetime.now(BJT) - timedelta(seconds=sc.SCAN_CACHE_TTL + 10)
        p = _fresh_payload(updated=old.isoformat(timespec="seconds"))
        self.assertFalse(sc.is_fresh_scan_cache(p))

    def test_incomplete_checkpoint_not_fresh(self):
        p = _fresh_payload(done=False)
        self.assertFalse(sc.is_fresh_scan_cache(p))

    def test_as_of_fallback(self):
        now = datetime.now(BJT)
        p = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "scope": "market",
            "done": True,
            "candidates": [{"code": "1"}],
        }
        self.assertTrue(sc.is_fresh_scan_cache(p))

    def test_decorate_adds_updated_and_items(self):
        p = sc.decorate_scan_payload({"candidates": [{"code": "1"}], "as_of": "x"})
        self.assertIn("updated", p)
        self.assertEqual(p["items"], [{"code": "1"}])
        dt = sc.parse_updated(p)
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)


class ScanCacheResponseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.crypto = Path(self.tmp.name) / "crypto.json"
        self.p_watch = patch.object(server, "WATCH_FILE", self.watch)
        self.p_crypto = patch.object(sc, "CRYPTO_FILE", self.crypto)
        self.p_watch.start()
        self.p_crypto.start()
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        self.p_watch.stop()
        self.p_crypto.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def test_serves_fresh_market_cache(self):
        self.watch.write_text(json.dumps(_fresh_payload()), encoding="utf-8")
        hit = server.scan_cache_response("market", force=False)
        self.assertIsNotNone(hit)
        self.assertTrue(hit["from_cache"])
        self.assertEqual(hit["status"], "cached")
        self.assertEqual(hit["candidates"][0]["code"], "000001")
        self.assertEqual(hit["items"][0]["code"], "000001")

    def test_force_bypasses_cache(self):
        self.watch.write_text(json.dumps(_fresh_payload()), encoding="utf-8")
        self.assertIsNone(server.scan_cache_response("market", force=True))

    def test_pool_cache_not_used_for_market(self):
        self.watch.write_text(json.dumps(_fresh_payload(scope="pool", universe_size=None)), encoding="utf-8")
        self.assertIsNone(server.scan_cache_response("market", force=False))

    def test_crypto_cache(self):
        rows = [{"code": "BTCUSDT", "score": 80, "qualified": False, "market": "crypto"}]
        self.crypto.write_text(json.dumps(_fresh_payload(scope="crypto", candidates=rows, items=rows)),
                               encoding="utf-8")
        hit = server.scan_cache_response("crypto", force=False)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["candidates"][0]["code"], "BTCUSDT")


class ScanPostHttpTest(unittest.TestCase):
    """POST /api/scan 在 1h 内应直接 from_cache，且不得调用 fetch_universe。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.watch.write_text(json.dumps(_fresh_payload()), encoding="utf-8")
        self.patches = [
            patch.object(server, "WATCH_FILE", self.watch),
            patch.object(sc, "WATCH_FILE", self.watch),
        ]
        for p in self.patches:
            p.start()
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.universe_calls = []

        def boom(*_a, **_k):
            self.universe_calls.append(1)
            raise AssertionError("fetch_universe should not run on cache hit")

        self.p_uni = patch.object(sc, "fetch_universe", side_effect=boom)
        self.p_scan = patch.object(sc, "run_market_scan", side_effect=boom)
        self.p_uni.start()
        self.p_scan.start()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.p_uni.stop()
        self.p_scan.stop()
        for p in self.patches:
            p.stop()
        server.reset_scan_runtime_state()
        self.tmp.cleanup()

    def _post(self, body: dict, query: str = ""):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/api/scan{query}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_post_within_1h_returns_from_cache(self):
        out = self._post({"mode": "market"})
        self.assertTrue(out.get("from_cache"))
        self.assertEqual(out.get("status"), "cached")
        self.assertEqual(self.universe_calls, [])
        self.assertEqual(out["candidates"][0]["code"], "000001")
        self.assertIn("scan_progress", out)
        self.assertFalse(out.get("scanning"))

    def test_post_force_query_does_not_use_cache(self):
        out = self._post({"mode": "market", "force": True}, query="?force=1")
        # force 会启动后台扫描；缓存短路不应发生
        self.assertFalse(out.get("from_cache"))
        self.assertEqual(out.get("status"), "started")


class DataSourcePatchTest(unittest.TestCase):
    def test_universe_sina_uses_sh_a_sz_a_not_hs_a(self):
        seen = []

        def fake_json(url, timeout=10.0):
            seen.append(url)
            if "node=sh_a" in url or "node=sz_a" in url:
                # 返回一页后结束
                return []
            raise AssertionError(f"unexpected url {url}")

        with patch.object(sc, "http_json", side_effect=fake_json):
            sc._universe_sina()
        self.assertTrue(any("node=sh_a" in u for u in seen))
        self.assertTrue(any("node=sz_a" in u for u in seen))
        self.assertFalse(any("node=hs_a" in u for u in seen))

    def test_universe_sina_price_falls_back_to_settlement(self):
        page = [{
            "symbol": "sh600000", "code": "600000", "name": "浦发银行",
            "trade": "0.000", "settlement": "8.50", "changepercent": "0.1",
            "turnoverratio": "1.2", "amount": "1", "mktcap": "1",
        }]
        calls = {"n": 0}

        def fake_json(url, timeout=10.0):
            calls["n"] += 1
            if calls["n"] == 1:
                return page
            return []

        with patch.object(sc, "http_json", side_effect=fake_json):
            rows = sc._universe_sina_node("sh_a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 8.5)

    def test_norm_crypto_symbol(self):
        self.assertEqual(sc.norm_crypto_symbol("BTC_USDT"), "BTCUSDT")
        self.assertEqual(sc.gate_contract("BTCUSDT"), "BTC_USDT")

    def test_crypto_sticky_gate_on_binance_fail(self):
        sc.reset_crypto_backend()

        def fake_json(url, timeout=10.0):
            if "binance" in url or "fapi.binance.com" in url:
                raise TimeoutError("binance down")
            if "gateio" in url and url.endswith("/tickers"):
                return [{
                    "contract": "BTC_USDT", "last": "65000",
                    "change_percentage": "2.5", "volume_24h_quote": "1",
                }]
            raise AssertionError(url)

        with patch.object(sc, "http_json", side_effect=fake_json):
            rows = sc.fetch_crypto_tickers()
        self.assertEqual(sc._crypto_backend, "gate")
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["price"], 65000.0)

        def kline_json(url, timeout=10.0):
            self.assertIn("gateio", url)
            self.assertIn("BTC_USDT", url)
            return [{"t": 1700000000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"}]

        with patch.object(sc, "http_json", side_effect=kline_json):
            bars = sc.fetch_crypto_kline("BTCUSDT")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 1.5)


if __name__ == "__main__":
    unittest.main()
