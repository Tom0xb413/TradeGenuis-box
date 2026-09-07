#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多模式箱体：classic 回归、P0 门控/分位数/试盘、P1 骨架、配置与 K 线接线。不跑全市场扫描。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import box_engine as be  # noqa: E402
import scanner as sc  # noqa: E402
import server  # noqa: E402

BJT = timezone(timedelta(hours=8))


def _bar(i: int, *, o, h, l, c, vol=1000.0, d0=None) -> dict:
    d0 = d0 or date(2024, 1, 2)
    return {
        "date": (d0 + timedelta(days=i)).isoformat(),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c),
        "vol": float(vol),
    }


def _flat(n: int = 80, *, px=10.0, hi=10.20, lo=9.80, vol=1000.0) -> list[dict]:
    return [_bar(i, o=px, h=hi, l=lo, c=px, vol=vol) for i in range(n)]


def _trend(n: int = 80, *, start=10.0, step=0.30) -> list[dict]:
    out = []
    for i in range(n):
        px = start + i * step
        out.append(_bar(i, o=px, h=px + 0.12, l=px - 0.12, c=px, vol=1000.0))
    return out


class ClassicRegressionTest(unittest.TestCase):
    def test_default_mode_is_classic(self):
        bars = _flat()
        a = sc.compute_box(bars)
        b = sc.compute_box_classic(bars)
        self.assertEqual(a["box_high"], b["box_high"])
        self.assertEqual(a["box_low"], b["box_low"])
        self.assertEqual(a["tests"], b["tests"])
        self.assertEqual(a["box_mode"], "classic")

    def test_flat_series_uses_max_min_edges(self):
        bars = _flat()
        box = sc.compute_box(bars, mode="classic")
        self.assertIsNotNone(box)
        self.assertEqual(box["box_high"], 10.20)
        self.assertEqual(box["box_low"], 9.80)
        self.assertEqual(box["box_mode"], "classic")

    def test_unknown_mode_falls_back_classic(self):
        self.assertEqual(sc.normalize_box_mode("nope"), "classic")
        self.assertEqual(sc.normalize_box_mode(None), "classic")
        bars = _flat()
        box = sc.compute_box(bars, mode="??")
        self.assertEqual(box["box_mode"], "classic")
        self.assertEqual(box["box_high"], 10.20)


class P0QuantileAndGateTest(unittest.TestCase):
    def test_quantile_edges_ignore_spike(self):
        bars = _flat()
        bars[40]["high"] = 18.0
        classic = sc.compute_box(bars, mode="classic")
        p0 = sc.compute_box(bars, mode="p0")
        self.assertEqual(classic["box_high"], 18.0)
        self.assertLess(p0["box_high"], 12.0)
        self.assertAlmostEqual(p0["box_high"], 10.20, delta=0.05)
        self.assertAlmostEqual(p0["box_low"], 9.80, delta=0.05)
        self.assertNotEqual(p0["box_quality"], "amplitude_reject")

    def test_amplitude_gate_rejects_steep_trend(self):
        bars = _trend()
        classic = sc.compute_box(bars, mode="classic")
        p0 = sc.compute_box(bars, mode="p0")
        self.assertIsNotNone(classic)
        self.assertGreater(classic["box_high"] - classic["box_low"], 1.0)
        self.assertEqual(p0["tests"], 0)
        self.assertEqual(p0["box_quality"], "amplitude_reject")
        self.assertGreaterEqual(p0["amp_pct"], be.P0_AMP_MAX * 100)

    def test_p0_amp_constant_documented(self):
        self.assertEqual(be.P0_AMP_MAX, 0.15)
        self.assertEqual(be.P0_HIGH_Q, 0.95)
        self.assertEqual(be.P0_LOW_Q, 0.05)


class TestCountRulesTest(unittest.TestCase):
    def _series_with_mixed_tests(self) -> list[dict]:
        bars = _flat()
        # classic 会认、P0 不认：贴上沿但上影极短、量仅略高于 classic 门槛
        for i in (35, 45, 55):
            bars[i] = _bar(i, o=10.15, h=10.20, l=9.80, c=10.18, vol=800.0)
        # P0 严规则通过：刺上沿、收盘回到内侧、长上影、1.8× 前 20 日均量
        bars[60] = _bar(60, o=10.05, h=10.20, l=10.00, c=10.02, vol=2000.0)
        return bars

    def test_p0_counts_fewer_than_classic_on_weak_wicks(self):
        bars = self._series_with_mixed_tests()
        classic = sc.compute_box(bars, mode="classic")
        p0 = sc.compute_box(bars, mode="p0")
        self.assertGreaterEqual(classic["tests"], 3)
        self.assertEqual(p0["tests"], 1)
        self.assertIn(bars[60]["date"], p0["test_dates"])
        self.assertEqual(p0["box_quality"], "weak")
        self.assertIn("post_hold_tests", p0)

    def test_p0_volume_and_shadow_required(self):
        bars = _flat()
        # 长上影但量能不够
        bars[50] = _bar(50, o=10.05, h=10.20, l=10.00, c=10.02, vol=1100.0)
        p0 = sc.compute_box(bars, mode="p0")
        self.assertEqual(p0["tests"], 0)


class P1StubTest(unittest.TestCase):
    def test_p1_is_enum_and_falls_back(self):
        self.assertIn("p1", sc.BOX_MODES)
        bars = _flat()
        out = sc.compute_box(bars, mode="p1")
        self.assertEqual(out["box_mode"], "p1")
        self.assertEqual(out["p1_status"], "not_implemented")
        self.assertEqual(out["box_high"], 10.20)
        self.assertEqual(out["box_quality"], "p1_pending")
        self.assertIn("尚未实现", out["note"])

    def test_compute_box_p1_callable(self):
        self.assertTrue(callable(sc.compute_box_p1))
        self.assertTrue(callable(sc.compute_box_p0))


class ConfigAndKlineWireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.disk = Path(self.tmp.name) / "kline"
        self.watch = Path(self.tmp.name) / "watchlist.json"
        server.STATE["kline_cache"] = {}
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.calls = {"kline": 0}

        def fake_kline(code, lmt=160):
            self.calls["kline"] += 1
            return _flat()

        self.patches = [
            patch.object(server, "CONFIG_FILE", self.cfg_path),
            patch.object(server, "KLINE_DISK_DIR", self.disk),
            patch.object(server, "WATCH_FILE", self.watch),
            patch.object(sc, "WATCH_FILE", self.watch),
            patch.object(sc, "DATA", Path(self.tmp.name)),
            patch.object(sc, "fetch_quote", return_value={
                "price": 10.0, "chg": 0.0, "name": "测", "turnover": 1.0, "volume_ratio": 1.0,
            }),
            patch.object(sc, "fetch_kline", side_effect=fake_kline),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        server.STATE["kline_cache"] = {}
        self.tmp.cleanup()

    def test_default_config_has_classic(self):
        self.assertEqual(server.DEFAULT_CONFIG["box_mode"], "classic")
        self.assertEqual(server.configured_box_mode(), "classic")

    def test_kline_box_follows_config_without_refetch(self):
        a = server.get_kline("600000")
        self.assertEqual(a["box_mode"], "classic")
        self.assertEqual(a["box"]["box_high"], 10.20)
        self.assertEqual(self.calls["kline"], 1)
        server.STATE["config"]["box_mode"] = "p0"
        b = server.get_kline("600000")
        self.assertEqual(self.calls["kline"], 1)
        self.assertEqual(b["box_mode"], "p0")
        self.assertEqual(b["box"]["box_mode"], "p0")

    def test_scan_cache_miss_when_box_mode_differs(self):
        now = datetime.now(BJT)
        rows = [{"code": "000001", "score": 90, "qualified": True}]
        payload = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": now.isoformat(timespec="seconds"),
            "scope": "market",
            "done": True,
            "universe_size": 10,
            "candidates": rows,
            "items": rows,
            "box_mode": "classic",
        }
        self.watch.write_text(json.dumps(payload), encoding="utf-8")
        server.STATE["config"]["box_mode"] = "classic"
        hit = server.scan_cache_response("market", force=False)
        self.assertIsNotNone(hit)
        server.STATE["config"]["box_mode"] = "p0"
        self.assertIsNone(server.scan_cache_response("market", force=False))


class ConfigHttpTest(unittest.TestCase):
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

    def test_get_post_box_mode(self):
        got = self._req("GET", "api/config")
        self.assertEqual(got["box_mode"], "classic")
        self.assertEqual(got["box_modes"], ["classic", "p0", "p1"])
        saved = self._req("POST", "api/config", {"box_mode": "p0"})
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["config"]["box_mode"], "p0")
        got2 = self._req("GET", "api/config")
        self.assertEqual(got2["box_mode"], "p0")
        disk = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["box_mode"], "p0")

    def test_p1_accepted_in_api_enum(self):
        saved = self._req("POST", "api/config", {"box_mode": "p1"})
        self.assertEqual(saved["config"]["box_mode"], "p1")

    def test_invalid_mode_coerced_classic(self):
        saved = self._req("POST", "api/config", {"box_mode": "banana"})
        self.assertEqual(saved["config"]["box_mode"], "classic")


class DashboardBoxModeContractTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_mode_selector_labels(self):
        self.assertIn("箱体模式", self.html)
        self.assertIn("data-box-mode=\"classic\"", self.html)
        self.assertIn("P0增强", self.html)
        self.assertIn("P1通道(soon)", self.html)
        self.assertIn("setBoxMode", self.html)
        self.assertIn("即将推出", self.html)

    def test_p1_disabled(self):
        self.assertIn('data-box-mode="p1"', self.html)
        self.assertRegex(self.html, r'data-box-mode="p1"[^>]*disabled')


class LoadBoxModeFileTest(unittest.TestCase):
    def test_reads_config_json(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"box_mode": "p0"}), encoding="utf-8")
            with patch.object(sc, "DATA", data):
                self.assertEqual(sc.load_box_mode(), "p0")
            with patch.object(sc, "DATA", data / "missing"):
                self.assertEqual(sc.load_box_mode(), "classic")


class BoxRowFieldsTest(unittest.TestCase):
    def test_none_box(self):
        f = sc.box_row_fields(None, "p0")
        self.assertEqual(f["tests"], 0)
        self.assertEqual(f["box_mode"], "p0")
        self.assertIsNone(f["box_high"])

    def test_maps_span_and_quality(self):
        box = sc.compute_box(_flat(), mode="p0")
        f = sc.box_row_fields(box, "p0")
        self.assertEqual(f["box_span_pct"], box["span_pct"])
        self.assertEqual(f["box_quality"], box.get("box_quality"))


if __name__ == "__main__":
    unittest.main()
