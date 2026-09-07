#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描并发配置、结构化进度状态机、扫描中加入。不跑全市场扫描。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scanner as sc  # noqa: E402
import server  # noqa: E402

DASH = ROOT / "dashboard.html"
PROGRESS_KEYS = (
    "running", "mode", "phase", "done", "total", "pct",
    "message", "started_at", "updated_at",
)


def _score_stub(code: str, name: str, box_mode: str = "classic") -> dict:
    return sc.score_row({
        "code": code, "name": name, "price": 10.0, "chg": 1.0,
        "theme_ok": False, "volume_days": 0, "volume_ratio": 0.0,
        **sc.box_row_fields(None, box_mode),
        **sc.flag_row_fields(None),
        "fund_state": "无数据", "control": "中",
    })


class ScanWorkersTest(unittest.TestCase):
    def test_clamp_range(self):
        self.assertEqual(sc.clamp_scan_workers(1), 4)
        self.assertEqual(sc.clamp_scan_workers(4), 4)
        self.assertEqual(sc.clamp_scan_workers(16), 16)
        self.assertEqual(sc.clamp_scan_workers(32), 32)
        self.assertEqual(sc.clamp_scan_workers(99), 32)
        self.assertEqual(sc.clamp_scan_workers("8"), 8)
        self.assertEqual(sc.clamp_scan_workers("nope"), sc.SCAN_WORKERS_DEFAULT)

    def test_explicit_wins_over_env(self):
        with patch.dict(os.environ, {"SCAN_WORKERS": "24"}):
            self.assertEqual(sc.resolve_scan_workers(12), 12)
            self.assertEqual(sc.resolve_scan_workers(1), 4)
            self.assertEqual(sc.resolve_scan_workers(100), 32)

    def test_env_overrides_config(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"scan_workers": 8}), encoding="utf-8")
            with patch.object(sc, "DATA", data), patch.dict(os.environ, {"SCAN_WORKERS": "24"}):
                self.assertEqual(sc.resolve_scan_workers(None), 24)

    def test_config_without_env(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"scan_workers": 8}), encoding="utf-8")
            with patch.object(sc, "DATA", data), patch.dict(os.environ):
                os.environ.pop("SCAN_WORKERS", None)
                os.environ.pop("scan_workers", None)
                self.assertEqual(sc.resolve_scan_workers(None), 8)

    def test_default_16(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(sc, "DATA", Path(td)), patch.dict(os.environ):
                os.environ.pop("SCAN_WORKERS", None)
                os.environ.pop("scan_workers", None)
                self.assertEqual(sc.resolve_scan_workers(None), 16)
                self.assertEqual(sc.SCAN_WORKERS_DEFAULT, 16)
                self.assertEqual(sc.MARKET_WORKERS, 16)


class PoolScanParallelTest(unittest.TestCase):
    def test_run_scan_uses_thread_pool_and_progress(self):
        pool = [
            {"code": "000001", "name": "一", "theme": ""},
            {"code": "000002", "name": "二", "theme": ""},
            {"code": "600000", "name": "三", "theme": ""},
        ]
        seen = {}
        events = []

        def fake_ex(*args, **kwargs):
            seen["max_workers"] = kwargs.get("max_workers")
            return ThreadPoolExecutor(*args, **kwargs)

        def fake_analyze(code, name, theme, hot_names, box_mode=None):
            return _score_stub(code, name, box_mode or "classic")

        def prog(msg, **kw):
            events.append((msg, kw))

        with tempfile.TemporaryDirectory() as td:
            watch = Path(td) / "watchlist.json"
            with patch.object(sc, "load_pool", return_value=pool), \
                 patch.object(sc, "fetch_hot_topics", return_value=([], set())), \
                 patch.object(sc, "analyze", side_effect=fake_analyze), \
                 patch.object(sc, "WATCH_FILE", watch), \
                 patch.object(sc, "DATA", Path(td)), \
                 patch("scanner.ThreadPoolExecutor", side_effect=fake_ex):
                rows = sc.run_scan(network=True, progress=prog, workers=16)
            self.assertTrue(watch.exists())
            payload = json.loads(watch.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("scope"), "pool")
            self.assertEqual(payload.get("pattern_family"), "box")
        self.assertEqual(seen.get("max_workers"), 16)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(e[1].get("phase") == "analyze" for e in events))
        self.assertEqual(max(e[1].get("done") or 0 for e in events), 3)
        self.assertTrue(any(e[1].get("total") == 3 for e in events))

    def test_run_scan_legacy_progress_str_only(self):
        """旧回调只接收 str，不应因 kwargs 崩溃。"""
        pool = [{"code": "000001", "name": "一", "theme": ""}]
        msgs = []
        with tempfile.TemporaryDirectory() as td:
            watch = Path(td) / "watchlist.json"
            with patch.object(sc, "load_pool", return_value=pool), \
                 patch.object(sc, "fetch_hot_topics", return_value=([], set())), \
                 patch.object(sc, "analyze",
                              side_effect=lambda *a, **k: _score_stub("000001", "一")), \
                 patch.object(sc, "WATCH_FILE", watch), \
                 patch.object(sc, "DATA", Path(td)):
                sc.run_scan(network=True, progress=lambda m: msgs.append(m), workers=4)
        self.assertTrue(msgs)

    def test_market_scan_workers_without_universe_fetch(self):
        stocks = [
            {"code": "000001", "name": "一", "price": 10.0, "chg": 1.0,
             "turnover": 1.0, "vr": 2.0, "amount": 1.0, "mv": 1.0},
            {"code": "000002", "name": "二", "price": 11.0, "chg": 2.0,
             "turnover": 1.0, "vr": 2.0, "amount": 1.0, "mv": 1.0},
        ]
        seen = {}
        events = []

        def fake_ex(*args, **kwargs):
            seen["max_workers"] = kwargs.get("max_workers")
            return ThreadPoolExecutor(*args, **kwargs)

        def prog(msg, **kw):
            events.append(kw)

        with tempfile.TemporaryDirectory() as td:
            watch = Path(td) / "watchlist.json"
            with patch.object(sc, "fetch_universe", return_value=stocks), \
                 patch.object(sc, "load_pool", return_value=[]), \
                 patch.object(sc, "fetch_hot_topics", return_value=([], set())), \
                 patch.object(sc, "analyze_market",
                              side_effect=lambda s, *_a, **_k: _score_stub(s["code"], s["name"])), \
                 patch.object(sc, "WATCH_FILE", watch), \
                 patch.object(sc, "DATA", Path(td)), \
                 patch("scanner.ThreadPoolExecutor", side_effect=fake_ex):
                rows = sc.run_market_scan(full=True, workers=16, progress=prog)
            self.assertEqual(len(rows), 2)
        self.assertEqual(seen.get("max_workers"), 16)
        self.assertTrue(any(e.get("phase") == "analyze" and e.get("total") == 2 for e in events))
        self.assertEqual(max(e.get("done") or 0 for e in events), 2)


class ScanProgressStateTest(unittest.TestCase):
    def setUp(self):
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def test_begin_update_finish(self):
        with server.LOCK:
            server.begin_scan_progress("pool")
        self.assertTrue(server.STATE["scanning"])
        sp = server.STATE["scan_progress"]
        self.assertTrue(sp["running"])
        self.assertEqual(sp["mode"], "pool")
        self.assertEqual(sp["phase"], "start")
        self.assertIsNotNone(sp["started_at"])
        for k in PROGRESS_KEYS:
            self.assertIn(k, sp)

        server.update_scan_progress(message="分析 3/10", phase="analyze", done=3, total=10)
        sp = server.STATE["scan_progress"]
        self.assertEqual(sp["done"], 3)
        self.assertEqual(sp["total"], 10)
        self.assertEqual(sp["pct"], 30.0)
        self.assertEqual(sp["phase"], "analyze")
        self.assertEqual(sp["message"], "分析 3/10")
        self.assertTrue(sp["running"])

        server.finish_scan_progress(True, "扫描完成：10 只，达标 1 只")
        self.assertFalse(server.STATE["scanning"])
        sp = server.STATE["scan_progress"]
        self.assertFalse(sp["running"])
        self.assertEqual(sp["phase"], "done")
        self.assertEqual(sp["pct"], 100.0)
        self.assertEqual(sp["done"], 10)
        self.assertIsNotNone(server.STATE["last_scan"])

    def test_finish_error_keeps_partial_pct(self):
        with server.LOCK:
            server.begin_scan_progress("market")
        server.update_scan_progress(message="一半", phase="analyze", done=5, total=10)
        server.finish_scan_progress(False, "扫描失败: boom")
        sp = server.STATE["scan_progress"]
        self.assertEqual(sp["phase"], "error")
        self.assertFalse(sp["running"])
        self.assertEqual(sp["pct"], 50.0)
        self.assertIsNone(server.STATE["last_scan"])

    def test_status_payload_always_has_progress(self):
        st = server.status_payload()
        self.assertIn("scanning", st)
        self.assertIn("scan_progress", st)
        self.assertIn("scan_log", st)
        self.assertFalse(st["scanning"])
        for k in PROGRESS_KEYS:
            self.assertIn(k, st["scan_progress"])


class ScanJoinHttpTest(unittest.TestCase):
    """POST 扫描中加入；阻塞的是假 run_market_scan，不拉 universe。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = Path(self.tmp.name) / "watchlist.json"
        server.reset_scan_runtime_state()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.scan_calls = []
        self.gate = threading.Event()

        def fake_market(*_a, **k):
            self.scan_calls.append(k)
            prog = k.get("progress")
            if prog:
                try:
                    prog("深度计算 2/8", phase="analyze", done=2, total=8)
                except TypeError:
                    prog("深度计算 2/8")
            self.gate.wait(timeout=8)
            return [{"code": "000001", "qualified": True, "score": 90}]

        self.patches = [
            patch.object(server, "WATCH_FILE", self.watch),
            patch.object(sc, "WATCH_FILE", self.watch),
            patch.object(sc, "run_market_scan", side_effect=fake_market),
            patch.object(sc, "fetch_universe", side_effect=AssertionError("no universe")),
        ]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.gate.set()
        self.httpd.shutdown()
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

    def _get(self, path: str):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_status_idle_includes_scan_progress(self):
        st = self._get("api/status")
        self.assertFalse(st["scanning"])
        for k in PROGRESS_KEYS:
            self.assertIn(k, st["scan_progress"])
        self.assertIn("scan_log", st)

    def test_join_while_running_does_not_start_second_job(self):
        out1 = self._post({"mode": "market", "force": True})
        self.assertEqual(out1["status"], "started")
        self.assertTrue(out1.get("scanning"))
        self.assertTrue(out1["scan_progress"]["running"])
        self.assertEqual(out1["scan_progress"]["mode"], "market")

        deadline = time.time() + 3
        st = None
        while time.time() < deadline:
            st = self._get("api/status")
            if st["scan_progress"].get("total") == 8:
                break
            time.sleep(0.05)
        self.assertIsNotNone(st)
        self.assertTrue(st["scanning"])
        self.assertEqual(st["scan_progress"]["done"], 2)
        self.assertEqual(st["scan_progress"]["total"], 8)
        self.assertEqual(st["scan_progress"]["pct"], 25.0)
        self.assertEqual(st["scan_progress"]["mode"], "market")

        out2 = self._post({"mode": "market", "force": True})
        self.assertEqual(out2["status"], "running")
        self.assertTrue(out2["scanning"])
        self.assertIn("scan_progress", out2)
        self.assertEqual(out2["scan_progress"]["mode"], "market")
        self.assertEqual(len(self.scan_calls), 1)

        self.gate.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            st = self._get("api/status")
            if not st["scanning"]:
                break
            time.sleep(0.05)
        self.assertFalse(st["scanning"])
        self.assertEqual(st["scan_progress"]["phase"], "done")
        self.assertEqual(st["scan_progress"]["pct"], 100.0)
        self.assertFalse(st["scan_progress"]["running"])

    def test_injected_scanning_joins_without_worker(self):
        with server.LOCK:
            server.begin_scan_progress("crypto")
            server.update_scan_progress(message="币圈 1/30", phase="analyze", done=1, total=30)
        out = self._post({"mode": "crypto", "force": True})
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["scan_progress"]["mode"], "crypto")
        self.assertEqual(out["scan_progress"]["done"], 1)
        self.assertEqual(len(self.scan_calls), 0)


class DashboardProgressContractTest(unittest.TestCase):
    def setUp(self):
        self.html = DASH.read_text(encoding="utf-8")

    def test_shared_progress_bar_and_poll(self):
        self.assertIn('id="scanProgress"', self.html)
        self.assertIn("scan_progress", self.html)
        self.assertIn("ensureScanPoll", self.html)
        self.assertIn("tickScanStatus", self.html)
        self.assertIn("setInterval(tickScanStatus, 1000)", self.html)
        self.assertIn("isScanRunning", self.html)
        self.assertIn("已加入当前扫描", self.html)
        self.assertIn("if (isScanRunning(S.status))", self.html)
        self.assertNotIn("setTimeout(res, 3000)", self.html)

    def test_box_and_flag_controls_untouched(self):
        self.assertIn('data-box-mode="classic"', self.html)
        self.assertIn('data-family="high_flag"', self.html)
        self.assertIn("setBoxMode", self.html)
        self.assertIn("setPatternFamily", self.html)


if __name__ == "__main__":
    unittest.main()
