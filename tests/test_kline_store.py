#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地滚动 K 线库：merge/trim 180、增量追加、状态、从 fixture 分析。不打 live Gate。"""
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

import kline_store as ks  # noqa: E402
import sync_worker as sw  # noqa: E402
import scanner as sc  # noqa: E402
import global_pool as gp  # noqa: E402
import server  # noqa: E402

BJT = timezone(timedelta(hours=8))
DASH = ROOT / "dashboard.html"


def _daily_bars(n: int, start: str = "2024-01-02") -> list[dict]:
    d0 = datetime.strptime(start, "%Y-%m-%d")
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


class MergeTrimTest(unittest.TestCase):
    def test_trim_to_180(self):
        bars = _daily_bars(200)
        out = ks.merge_trim_bars([], bars, cap=180)
        self.assertEqual(len(out), 180)
        self.assertEqual(out[0]["date"], bars[-180]["date"])
        self.assertEqual(out[-1]["date"], bars[-1]["date"])

    def test_incoming_overwrites_same_date(self):
        old = _daily_bars(3)
        new = [dict(old[1], close=99.0, high=99.5)]
        out = ks.merge_trim_bars(old, new, cap=180)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1]["close"], 99.0)

    def test_incremental_append_with_overlap(self):
        old = _daily_bars(10)
        incoming = old[-3:] + _daily_bars(2, start="2024-01-12")
        out = ks.merge_trim_bars(old, incoming, cap=180)
        self.assertEqual(len(out), 12)
        self.assertEqual(out[0]["date"], "2024-01-02")
        self.assertEqual(out[-1]["date"], "2024-01-13")

    def test_lookback_empty_is_cap(self):
        self.assertEqual(ks.incremental_lookback(None, "4h", cap=180), 180)

    def test_lookback_recent_is_small(self):
        now = int(datetime.now(timezone.utc).timestamp())
        n = ks.incremental_lookback(now - 8 * 3600, "4h", cap=180, overlap=3)
        self.assertGreaterEqual(n, 4)
        self.assertLessEqual(n, 12)


class SqliteStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kline_store.sqlite"
        ks.reset_schema_cache()
        self.p = patch.object(ks, "DB_PATH", self.db)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        ks.reset_schema_cache()
        self.tmp.cleanup()

    def test_replace_trim_and_last_ts(self):
        bars = _daily_bars(200)
        merged = ks.replace_bars("BTCUSDT", "1d", bars, cap=180)
        self.assertEqual(len(merged), 180)
        self.assertEqual(ks.bar_count("BTCUSDT", "1d"), 180)
        stored = ks.get_bars("BTCUSDT", "1d")
        self.assertEqual(len(stored), 180)
        self.assertEqual(ks.last_bar_ts("BTCUSDT", "1d"), stored[-1]["ts"])

    def test_incremental_sqlite_append(self):
        first = _daily_bars(10)
        ks.replace_bars("ETHUSDT", "4h", first, cap=180)
        extra = _daily_bars(2, start="2024-01-12")
        ks.replace_bars("ETHUSDT", "4h", extra, cap=180)
        out = ks.get_bars("ETHUSDT", "4h")
        self.assertEqual(len(out), 12)

    def test_status_payload_shape(self):
        ks.replace_bars("BTCUSDT", "1d", _daily_bars(50), cap=180)
        ks.set_pull_status("BTCUSDT", "1d", last_success="2024-06-01T12:00:00+08:00",
                          last_error="", bar_count=50, source="crypto", lag_sec=120)
        ks.set_pull_status("FAIL", "1d", last_error="empty kline", bar_count=0)
        overall = ks.overall_status("1d")
        self.assertEqual(overall["interval"], "1d")
        self.assertEqual(overall["bar_cap"], 180)
        self.assertEqual(overall["error_count"], 1)
        codes = {r["symbol"] for r in overall["symbols"]}
        self.assertIn("BTCUSDT", codes)
        self.assertIn("FAIL", codes)


class AnalyzeFromStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kline_store.sqlite"
        self.crypto = Path(self.tmp.name) / "crypto.json"
        self.ov = Path(self.tmp.name) / "global_override_pool.json"
        ks.reset_schema_cache()
        self.patches = [
            patch.object(ks, "DB_PATH", self.db),
            patch.object(sc, "CRYPTO_FILE", self.crypto),
            patch.object(sc, "DATA", Path(self.tmp.name)),
            patch.object(gp, "OVERRIDE_FILE", self.ov),
            patch.object(sc, "load_crypto_interval", return_value="1d"),
            patch.object(sc, "load_box_mode", return_value="classic"),
            patch.object(sc, "load_pattern_family", return_value="box"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ks.reset_schema_cache()
        self.tmp.cleanup()

    def test_analysis_from_fixture_bars_no_live_fetch(self):
        bars = _daily_bars(50)
        ks.replace_bars("BTCUSDT", "1d", bars, cap=180)
        ks.job_set("universe_snapshot", [{
            "code": "BTCUSDT", "name": "BTCUSDT", "asset_class": "crypto",
            "source": "crypto", "market": "crypto",
        }])
        with patch.object(sc, "fetch_global_instrument",
                          side_effect=AssertionError("no live Gate")), \
             patch.object(sc, "fetch_crypto_tickers",
                          side_effect=AssertionError("no tickers")):
            payload = sw.analyze_from_store(interval="1d", workers=4,
                                           crypto_file=self.crypto, path=self.db)
        self.assertTrue(self.crypto.is_file())
        self.assertEqual(payload["scope"], "crypto")
        self.assertTrue(payload["from_store"])
        self.assertEqual(payload["crypto_interval"], "1d")
        self.assertIn("analysis_as_of", payload)
        self.assertTrue(payload["candidates"])
        self.assertEqual(payload["candidates"][0]["code"], "BTCUSDT")
        self.assertIn("score", payload["candidates"][0])
        disk = json.loads(self.crypto.read_text(encoding="utf-8"))
        self.assertEqual(disk["items"][0]["code"], "BTCUSDT")

    def test_sync_merge_mocked_fetch(self):
        existing = _daily_bars(10)
        ks.replace_bars("BTCUSDT", "1d", existing, cap=180)
        gp.save_override_symbols(["BTCUSDT"])
        incoming = _daily_bars(3, start="2024-01-10")
        inst = {
            "code": "BTCUSDT", "name": "BTCUSDT", "price": 12, "chg": 1,
            "bars": incoming, "source": "crypto", "asset_class": "crypto",
        }
        with patch.object(sc, "fetch_crypto_tickers", return_value=[]), \
             patch.object(sc, "resolve_gold_instrument", return_value=None), \
             patch.object(sc, "fetch_global_instrument", return_value=inst), \
             patch.object(sc, "load_crypto_interval", return_value="1d"):
            res = sw.sync_symbols(interval="1d", workers=4, path=self.db)
        self.assertEqual(res["ok_count"], 1)
        stored = ks.get_bars("BTCUSDT", "1d", path=self.db)
        self.assertGreaterEqual(len(stored), 11)
        self.assertLessEqual(len(stored), 180)


class StoreStatusHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "kline_store.sqlite"
        self.ov = Path(self.tmp.name) / "global_override_pool.json"
        self.maint = Path(self.tmp.name) / "maintained_pool.json"
        ks.reset_schema_cache()
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        server.STATE["kline_cache"] = {}
        self.patches = [
            patch.object(ks, "DB_PATH", self.db),
            patch.object(ks, "MAINTAINED_FILE", self.maint),
            patch.object(gp, "OVERRIDE_FILE", self.ov),
            patch.object(sc, "crypto_symbol_is_listed", return_value=True),
            patch.object(sc, "gate_contract_is_listed", return_value=True),
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
        server.reset_scan_runtime_state()
        ks.reset_schema_cache()
        self.tmp.cleanup()

    def _req(self, method, path, body=None):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_status_has_overall_and_symbols(self):
        st = self._req("GET", "api/kline_store/status")
        self.assertIn("symbols", st)
        self.assertEqual(st["bar_cap"], 180)
        self.assertEqual(st["kline_sync_hours"], 4)
        self.assertIn("last_sync_end", st)
        self.assertIn("next_due", st)
        self.assertFalse(st["running"])
        self.assertIn("source_choices", st)
        self.assertGreaterEqual(st["pool_count"], 1)

    def test_pool_add_aapl_no_live_gate(self):
        out = self._req("POST", "api/kline_store/pool", {
            "action": "add", "symbols": ["AAPL"], "source": "gate",
        })
        self.assertTrue(out.get("ok"))
        codes = {x["code"] for x in out.get("symbols") or []}
        self.assertIn("AAPL", codes)
        row = next(x for x in out["symbols"] if x["code"] == "AAPL")
        self.assertEqual(row["source"], "gate")

    def test_kline_reads_store_without_fetch(self):
        bars = _daily_bars(50)
        ks.replace_bars("BTCUSDT", "1d", bars, cap=180)
        with patch.object(sc, "fetch_global_instrument",
                          side_effect=AssertionError("store should hit")):
            out = self._req("GET", "api/kline?code=BTCUSDT&market=crypto")
        self.assertNotIn("error", out)
        self.assertEqual(len(out["bars"]), 50)
        self.assertTrue(out.get("from_store"))
        self.assertIn("box", out)


class DashboardKlineStoreContractTest(unittest.TestCase):
    def setUp(self):
        self.html = DASH.read_text(encoding="utf-8")

    def test_main_scan_buttons_removed(self):
        self.assertNotIn('id="btnScan"', self.html)
        self.assertNotIn('id="btnForceScan"', self.html)
        self.assertNotIn("全市场标的扫描", self.html)
        self.assertIn("立即同步K线", self.html)
        self.assertIn("立即分析", self.html)
        self.assertIn("立即扫描 A 股", self.html)
        self.assertIn("标的/数据", self.html)
        self.assertIn("打开标的/数据", self.html)
        self.assertIn("api/kline_store/sync", self.html)
        self.assertIn("kline_sync_hours", self.html)


class GetKlineStoreFirstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "k.sqlite"
        self.disk = Path(self.tmp.name) / "kline_cache"
        ks.reset_schema_cache()
        server.STATE["kline_cache"] = {}
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.patches = [
            patch.object(ks, "DB_PATH", self.db),
            patch.object(server, "KLINE_DISK_DIR", self.disk),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server.STATE["kline_cache"] = {}
        ks.reset_schema_cache()
        self.tmp.cleanup()

    def test_store_hit_skips_upstream(self):
        ks.replace_bars("BTCUSDT", "1d", _daily_bars(40), cap=180)
        with patch.object(sc, "fetch_global_instrument",
                          side_effect=AssertionError("no fetch")):
            out = server.get_kline("BTCUSDT", market="crypto")
        self.assertEqual(len(out["bars"]), 40)
        self.assertTrue(out.get("from_store"))
        self.assertIsNotNone(out.get("box"))


if __name__ == "__main__":
    unittest.main()
