#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日 K 长缓存 + 看板懒加载契约。不触发全市场扫描。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scanner as sc  # noqa: E402
import server  # noqa: E402

DASH = ROOT / "dashboard.html"


def _bars(n: int = 80) -> list[dict]:
    d0 = date(2024, 1, 2)
    out = []
    for i in range(n):
        out.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "open": 10.0 + i * 0.01,
            "close": 10.1 + i * 0.01,
            "high": 10.3 + i * 0.01,
            "low": 9.9 + i * 0.01,
            "vol": 10000 + i,
        })
    return out


def _quote(code: str = "600000") -> dict:
    return {
        "price": 10.2, "chg": 1.1, "name": "测试" + code[-2:],
        "turnover": 1.2, "volume_ratio": 1.5,
    }


class DashboardLazyLoadContractTest(unittest.TestCase):
    """静态检查：进页不得对所有卡片立刻 loadChart。"""

    def setUp(self):
        self.html = DASH.read_text(encoding="utf-8")

    def test_uses_intersection_observer(self):
        self.assertIn("IntersectionObserver", self.html)
        self.assertIn("observeCardCharts", self.html)
        self.assertIn("KLINE_ROOT_MARGIN", self.html)

    def test_concurrency_capped_at_4(self):
        self.assertIn("KLINE_MAX_INFLIGHT = 4", self.html)
        self.assertNotIn("shown.forEach(r => loadChart", self.html)
        self.assertNotIn("filter(r => r.qualified).forEach(r => loadChart", self.html)

    def test_errors_not_cached_in_kbars(self):
        self.assertIn("if (klineOk(d)) S.kbars[job.key] = d", self.html)

    def test_drawchart_box_overlay_kept(self):
        self.assertIn("if (box.box_high != null)", self.html)
        self.assertIn("setLineDash", self.html)
        self.assertIn("function drawChart", self.html)

    def test_crypto_still_lists_pool(self):
        self.assertIn("币圈：展示进池子全部标的", self.html)
        self.assertIn("chartPriority", self.html)


class KlineCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disk = Path(self.tmp.name)
        server.STATE["kline_cache"] = {}
        self.patches = [
            patch.object(server, "KLINE_DISK_DIR", self.disk),
            patch.object(sc, "fetch_quote", side_effect=lambda code: _quote(code)),
        ]
        for p in self.patches:
            p.start()
        self.calls = {"kline": 0, "crypto": 0}

        def fake_kline(code, lmt=160):
            self.calls["kline"] += 1
            return _bars()

        def fake_crypto(code, limit=200, interval="1d"):
            self.calls["crypto"] += 1
            return _bars()

        self.p_k = patch.object(sc, "fetch_kline", side_effect=fake_kline)
        self.p_c = patch.object(sc, "fetch_crypto_kline", side_effect=fake_crypto)
        self.p_k.start()
        self.p_c.start()

    def tearDown(self):
        self.p_k.stop()
        self.p_c.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_ttl_in_30_to_60_minutes(self):
        self.assertGreaterEqual(server.KLINE_CACHE_TTL, 30 * 60)
        self.assertLessEqual(server.KLINE_CACHE_TTL, 60 * 60)

    def test_memory_hit_skips_upstream(self):
        a = server.get_kline("600000", lmt=100)
        b = server.get_kline("600000", lmt=100)
        self.assertEqual(self.calls["kline"], 1)
        self.assertEqual(a["code"], "600000")
        self.assertGreater(len(a["bars"]), 0)
        self.assertEqual(len(a["bars"]), len(b["bars"]))
        self.assertIsNotNone(a.get("box"))

    def test_disk_hit_after_memory_cleared(self):
        server.get_kline("600000")
        self.assertEqual(self.calls["kline"], 1)
        server.STATE["kline_cache"] = {}
        out = server.get_kline("600000")
        self.assertEqual(self.calls["kline"], 1)
        self.assertGreater(len(out["bars"]), 0)
        self.assertTrue(any(self.disk.glob("stock_600000_*.json")))

    def test_ttl_expiry_refetches(self):
        t0 = [1_700_000_000.0]

        def fake_time():
            return t0[0]

        with patch("server.time.time", side_effect=fake_time):
            server.get_kline("600000")
            self.assertEqual(self.calls["kline"], 1)
            t0[0] += server.KLINE_CACHE_TTL + 1
            server.STATE["kline_cache"] = {}
            server.get_kline("600000")
        self.assertEqual(self.calls["kline"], 2)

    def test_error_payload_not_cached(self):
        self.p_k.stop()
        boom = patch.object(sc, "fetch_kline", side_effect=RuntimeError("upstream down"))
        boom.start()
        try:
            r1 = server.get_kline("600000")
            r2 = server.get_kline("600000")
        finally:
            boom.stop()
            self.p_k.start()
        self.assertIn("error", r1)
        self.assertIn("error", r2)
        self.assertFalse(server.STATE["kline_cache"])
        self.assertFalse(list(self.disk.glob("*.json")))

    def test_empty_bars_not_cached(self):
        self.p_k.stop()
        empty = patch.object(sc, "fetch_kline", return_value=[])
        empty.start()
        try:
            r = server.get_kline("600000")
        finally:
            empty.stop()
            self.p_k.start()
        self.assertEqual(r.get("error"), "empty kline")
        self.assertFalse(server.STATE["kline_cache"])
        self.assertFalse(list(self.disk.glob("*.json")))

    def test_crypto_memory_and_disk(self):
        a = server.get_kline("BTCUSDT", market="crypto")
        server.STATE["kline_cache"] = {}
        b = server.get_kline("BTCUSDT", market="crypto")
        self.assertEqual(self.calls["crypto"], 1)
        self.assertEqual(a["bars"][-1]["close"], b["bars"][-1]["close"])
        self.assertTrue(any(self.disk.glob("crypto_BTCUSDT_*.json")))

    def test_stale_error_file_is_ignored(self):
        path = server._kline_disk_path("stock", "600000")
        self.disk.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "ts": time.time(),
            "payload": {"code": "600000", "error": "nope", "bars": []},
        }), encoding="utf-8")
        out = server.get_kline("600000")
        self.assertNotIn("error", out)
        self.assertEqual(self.calls["kline"], 1)
        self.assertGreater(len(out["bars"]), 0)


class KlineHttpCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disk = Path(self.tmp.name)
        server.STATE["kline_cache"] = {}
        self.calls = {"n": 0}

        def fake_kline(code, lmt=160):
            self.calls["n"] += 1
            return _bars()

        self.patches = [
            patch.object(server, "KLINE_DISK_DIR", self.disk),
            patch.object(sc, "fetch_quote", side_effect=lambda code: _quote(code)),
            patch.object(sc, "fetch_kline", side_effect=fake_kline),
        ]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _get(self, path: str):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_second_get_is_cache_hit(self):
        a = self._get("api/kline?code=600000&lmt=100")
        b = self._get("api/kline?code=600000&lmt=100")
        self.assertEqual(self.calls["n"], 1)
        self.assertEqual(a["code"], "600000")
        self.assertEqual(len(a["bars"]), len(b["bars"]))
        self.assertGreater(len(a["bars"]), 0)

    def test_scan_cache_untouched(self):
        self.assertEqual(sc.SCAN_CACHE_TTL, 3600)


if __name__ == "__main__":
    unittest.main()
