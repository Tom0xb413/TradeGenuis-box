#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高位旗形检测、形态族配置/缓存、看板契约。不跑全市场扫描。"""
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

import pattern_flag as pf  # noqa: E402
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


def _base(n: int = 50, *, px=10.0, vol=1000.0) -> list[dict]:
    return [_bar(i, o=px, h=px + 0.04, l=px - 0.04, c=px, vol=vol) for i in range(n)]


def _pole_bar(i: int) -> dict:
    # +8% 实体、3× 均量；中轴约 10.435
    return _bar(i, o=10.0, h=10.88, l=9.99, c=10.80, vol=3000.0)


def _handle_bar(i: int, *, vol=900.0) -> dict:
    return _bar(i, o=10.72, h=10.82, l=10.68, c=10.74, vol=vol)


def _valid_handle_bars(handle_n: int = 8) -> list[dict]:
    bars = _base(50)
    bars.append(_pole_bar(50))
    for j in range(1, handle_n + 1):
        bars.append(_handle_bar(50 + j))
    return bars


def _deep_dip_bars() -> list[dict]:
    bars = _base(50)
    bars.append(_pole_bar(50))
    for j in range(1, 9):
        # 整段掉到 9.50 附近：振幅仍紧，但深度 > 8%
        bars.append(_bar(50 + j, o=9.55, h=9.62, l=9.50, c=9.56, vol=900.0))
    return bars


def _secondary_buy_bars() -> list[dict]:
    bars = _valid_handle_bars(8)
    # 站上旗面高 10.82，量 > 1.4×900
    bars.append(_bar(59, o=10.80, h=11.10, l=10.78, c=11.05, vol=1400.0))
    return bars


class HighFlagDetectTest(unittest.TestCase):
    def test_constants_documented(self):
        self.assertEqual(pf.POLE_BODY_PCT, 0.05)
        self.assertEqual(pf.POLE_VOL_MULT, 2.0)
        self.assertEqual(pf.HANDLE_MIN, 2)
        self.assertEqual(pf.HANDLE_MAX, 15)
        self.assertEqual(pf.HANDLE_DEPTH, 0.08)
        self.assertEqual(pf.HANDLE_VOL_DRY, 0.55)
        self.assertEqual(pf.HANDLE_RANGE_MAX, 0.08)
        self.assertEqual(pf.SECONDARY_VOL_MULT, 1.40)

    def test_tight_dry_handle_is_match(self):
        flag = pf.detect_high_flag(_valid_handle_bars())
        self.assertEqual(flag["pattern"], "high_flag")
        self.assertEqual(flag["flag_stage"], "handle")
        self.assertTrue(flag["is_handle_consolidation"])
        self.assertFalse(flag["secondary_breakout_buy"])
        self.assertEqual(flag["handle_len"], 8)
        self.assertLessEqual(flag["vol_dry_ratio"], 0.55)
        self.assertLessEqual(flag["retrace_depth"], 8.0)
        self.assertEqual(flag["pole_date"], "2024-02-21")
        self.assertIn(flag["flag_quality"], ("ok", "strong"))

    def test_deep_dip_is_not_handle(self):
        flag = pf.detect_high_flag(_deep_dip_bars())
        self.assertEqual(flag["pattern"], "none")
        self.assertFalse(flag["is_handle_consolidation"])
        self.assertFalse(flag["secondary_breakout_buy"])

    def test_secondary_break_is_buy(self):
        flag = pf.detect_high_flag(_secondary_buy_bars())
        self.assertEqual(flag["pattern"], "high_flag")
        self.assertEqual(flag["flag_stage"], "secondary_buy")
        self.assertTrue(flag["secondary_breakout_buy"])
        self.assertFalse(flag["is_handle_consolidation"])
        self.assertGreater(flag["handle_high"], 10.8)
        self.assertAlmostEqual(flag["secondary_break_level"], flag["handle_high"])

    def test_wet_volume_handle_rejected(self):
        bars = _base(50)
        bars.append(_pole_bar(50))
        for j in range(1, 9):
            bars.append(_handle_bar(50 + j, vol=2200.0))
        flag = pf.detect_high_flag(bars)
        self.assertEqual(flag["pattern"], "none")

    def test_limit_up_style_pole_via_prev_close(self):
        bars = _base(50, px=10.0, vol=1000.0)
        # 一字：实体 0，相对昨收 +10%，放量
        bars.append(_bar(50, o=11.0, h=11.0, l=11.0, c=11.0, vol=3000.0))
        for j in range(1, 7):
            bars.append(_bar(50 + j, o=10.92, h=11.02, l=10.88, c=10.95, vol=800.0))
        flag = pf.detect_high_flag(bars)
        self.assertEqual(flag["pattern"], "high_flag")
        self.assertEqual(flag["flag_stage"], "handle")

    def test_empty_and_short_series(self):
        self.assertEqual(pf.detect_high_flag([])["pattern"], "none")
        self.assertEqual(pf.detect_high_flag(_base(10))["pattern"], "none")

    def test_row_fields_keys(self):
        f = pf.flag_row_fields(None)
        for k in ("pattern", "flag_stage", "is_handle_consolidation",
                  "secondary_breakout_buy", "pole_date", "flag_quality"):
            self.assertIn(k, f)
        self.assertEqual(f["pattern"], "none")


class BoxScoreUntouchedTest(unittest.TestCase):
    def test_flag_metadata_does_not_change_score(self):
        base = {
            "theme_ok": True, "volume_days": 3, "volume_ratio": 2.0,
            "fund_state": "流入", "control": "高", "tests": 3,
        }
        a = sc.score_row(dict(base))
        b = sc.score_row({
            **base,
            "pattern": "high_flag",
            "flag_stage": "secondary_buy",
            "is_handle_consolidation": False,
            "secondary_breakout_buy": True,
        })
        self.assertEqual(a["score"], 100)
        self.assertEqual(a["score"], b["score"])

    def test_normalize_family(self):
        self.assertEqual(pf.normalize_pattern_family(None), "box")
        self.assertEqual(pf.normalize_pattern_family("HIGH_FLAG"), "high_flag")
        self.assertEqual(pf.normalize_pattern_family("nope"), "box")
        self.assertEqual(sc.normalize_pattern_family("high_flag"), "high_flag")


class PatternFamilyConfigTest(unittest.TestCase):
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

    def test_default_family_is_box(self):
        got = self._req("GET", "api/config")
        self.assertEqual(got["pattern_family"], "box")
        self.assertEqual(got["pattern_families"], ["box", "high_flag", "trendline"])
        self.assertEqual(got["box_mode"], "classic")

    def test_post_family_persists(self):
        saved = self._req("POST", "api/config", {"pattern_family": "high_flag"})
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["config"]["pattern_family"], "high_flag")
        got = self._req("GET", "api/config")
        self.assertEqual(got["pattern_family"], "high_flag")
        disk = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["pattern_family"], "high_flag")

    def test_invalid_family_coerced_box(self):
        saved = self._req("POST", "api/config", {"pattern_family": "cup"})
        self.assertEqual(saved["config"]["pattern_family"], "box")


class ScanCacheFamilyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.p_watch = patch.object(server, "WATCH_FILE", self.watch)
        self.p_watch.start()
        server.STATE["scanning"] = False
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        self.p_watch.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _payload(self, **extra):
        now = datetime.now(BJT)
        rows = [{"code": "000001", "score": 90, "qualified": True, "pattern": "none"}]
        payload = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": now.isoformat(timespec="seconds"),
            "scope": "market",
            "done": True,
            "universe_size": 10,
            "candidates": rows,
            "items": rows,
            "box_mode": "classic",
            "pattern_family": "box",
        }
        payload.update(extra)
        return payload

    def test_hit_when_family_matches(self):
        self.watch.write_text(json.dumps(self._payload()), encoding="utf-8")
        hit = server.scan_cache_response("market", force=False)
        self.assertIsNotNone(hit)

    def test_miss_when_family_differs(self):
        self.watch.write_text(json.dumps(self._payload(pattern_family="box")), encoding="utf-8")
        server.STATE["config"]["pattern_family"] = "high_flag"
        self.assertIsNone(server.scan_cache_response("market", force=False))

    def test_legacy_payload_without_family_is_box(self):
        p = self._payload()
        p.pop("pattern_family")
        self.watch.write_text(json.dumps(p), encoding="utf-8")
        server.STATE["config"]["pattern_family"] = "box"
        self.assertIsNotNone(server.scan_cache_response("market", force=False))
        server.STATE["config"]["pattern_family"] = "high_flag"
        self.assertIsNone(server.scan_cache_response("market", force=False))


class KlineFlagOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disk = Path(self.tmp.name) / "kline"
        server.STATE["kline_cache"] = {}
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.patches = [
            patch.object(server, "KLINE_DISK_DIR", self.disk),
            patch.object(sc, "fetch_quote", return_value={
                "price": 10.74, "chg": 0.0, "name": "测", "turnover": 1.0, "volume_ratio": 1.0,
            }),
            patch.object(sc, "fetch_kline", return_value=_valid_handle_bars()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        server.STATE["kline_cache"] = {}
        self.tmp.cleanup()

    def test_kline_includes_flag_without_refetch(self):
        a = server.get_kline("600000")
        self.assertEqual(a["pattern_family"], "box")
        self.assertEqual(a["flag"]["pattern"], "high_flag")
        self.assertEqual(a["box_mode"], "classic")
        server.STATE["config"]["pattern_family"] = "high_flag"
        b = server.get_kline("600000")
        self.assertEqual(b["pattern_family"], "high_flag")
        self.assertEqual(b["flag"]["flag_stage"], "handle")
        self.assertIsNotNone(b.get("box"))


class LoadPatternFamilyFileTest(unittest.TestCase):
    def test_reads_config_json(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"pattern_family": "high_flag", "box_mode": "p0"}),
                encoding="utf-8")
            with patch.object(sc, "DATA", data):
                self.assertEqual(sc.load_pattern_family(), "high_flag")
                self.assertEqual(sc.load_box_mode(), "p0")
            with patch.object(sc, "DATA", data / "missing"):
                self.assertEqual(sc.load_pattern_family(), "box")


class DecoratePayloadFamilyTest(unittest.TestCase):
    def test_stamps_family(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td)
            (data / "config.json").write_text(
                json.dumps({"pattern_family": "high_flag"}), encoding="utf-8")
            with patch.object(sc, "DATA", data):
                p = sc.decorate_scan_payload({"candidates": [{"code": "1"}], "as_of": "x"})
        self.assertEqual(p["pattern_family"], "high_flag")
        self.assertIn("box_mode", p)


class DashboardFamilyContractTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_family_selector_labels(self):
        self.assertIn("箱体/通道", self.html)
        self.assertIn("高位旗形(杯柄)", self.html)
        self.assertIn('data-family="box"', self.html)
        self.assertIn('data-family="high_flag"', self.html)
        self.assertIn("setPatternFamily", self.html)
        self.assertIn("仅旗形", self.html)
        self.assertIn("仅二次买点", self.html)
        self.assertIn("flagFilterWrap", self.html)
        self.assertIn("boxWrap.style.display", self.html)
        self.assertIn("titleBits[S.flagFilter]", self.html)

    def test_chart_marks_pole_and_handle(self):
        self.assertIn("二次 ", self.html)
        self.assertIn("handle_start", self.html)
        self.assertIn("Pole 放量突破", self.html)
        self.assertIn("pattern_family", self.html)

    def test_rescan_hint_on_family_mismatch(self):
        self.assertIn("请强制重扫以更新", self.html)
        self.assertIn("scanFam", self.html)


class AnalyzeAttachesFlagTest(unittest.TestCase):
    def test_crypto_row_has_flag_keys(self):
        bars = _valid_handle_bars()
        row = sc.analyze_crypto("BTCUSDT", 10.74, 1.2, bars, box_mode="classic")
        self.assertIsNotNone(row)
        self.assertEqual(row["pattern"], "high_flag")
        self.assertTrue(row["is_handle_consolidation"])
        self.assertGreaterEqual(row["score"], 0)


if __name__ == "__main__":
    unittest.main()
