#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradeGenuis · 箱体突破 本地看板服务器

  启动：  python3 server.py [--port 8808] [--host 127.0.0.1]
  访问：  http://127.0.0.1:8808

接口：
  GET  /                    看板页
  GET  /api/watchlist       最近一次 A 股扫描结果（磁盘缓存）
  GET  /api/crypto          最近一次币圈扫描结果（磁盘缓存）
  GET  /api/pool            自选池
  POST /api/pool            增删自选池
  GET  /api/kline?code=     个股日K（含箱体/试盘；成功结果约 45 分钟内存+磁盘缓存）
  POST /api/scan            触发扫描 {mode, force}；1 小时内默认返回缓存。
                            已在扫描时返回 {status:running, scan_progress}（加入当前任务，不开第二轮）
  GET  /api/status          扫描状态：scanning + scan_progress + 最近 scan_log
  GET/POST /api/config      配置（自动扫描 / Telegram / box_mode / pattern_family / scan_workers）

自动扫描调度：config.auto 开启时，每个交易日 11:30 与 15:00 自动执行全市场扫描（绕过 1h 缓存）。
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import scanner as sc

ROOT = Path(__file__).resolve().parent
WATCH_FILE = ROOT / "data" / "watchlist.json"
POOL_FILE = ROOT / "data" / "pool.json"
CONFIG_FILE = ROOT / "data" / "config.json"

DEFAULT_CONFIG = {
    "auto": True,                       # 自动扫描开关
    "auto_times": ["11:30", "15:00"],   # 交易日午间收盘 / 收盘
    "tg_token": "",
    "tg_chat": "",
    "box_mode": "classic",              # classic | p0 | p1（斜向通道）
    "pattern_family": "box",            # box | high_flag（默认 box，不打断现有用户）
    "scan_workers": sc.SCAN_WORKERS_DEFAULT,  # 4–32，亦可用环境变量 SCAN_WORKERS
}


def default_scan_progress() -> dict:
    """空闲时的结构化扫描进度（所有浏览器共享同一份 STATE）。"""
    return {
        "running": False,
        "mode": None,
        "phase": "",
        "done": 0,
        "total": 0,
        "pct": 0.0,
        "message": "",
        "started_at": None,
        "updated_at": None,
    }


STATE = {
    "scanning": False,
    "scan_log": [],
    "scan_progress": default_scan_progress(),
    "last_scan": None,
    "kline_cache": {},
    "quote_cache": {},         # code -> (ts, payload) 2.5s 内存缓存
    "auto_done": set(),        # 已触发的自动扫描时间键 "YYYY-MM-DD HH:MM"
    "config": dict(DEFAULT_CONFIG),
}
LOCK = threading.RLock()
# 日K 变化慢：内存 + 落盘约 45 分钟。与 SCAN_CACHE_TTL（扫描结果 1h）独立。
KLINE_CACHE_TTL = 45 * 60
KLINE_DISK_DIR = ROOT / "data" / "kline_cache"


def as_truthy(v) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_query(path: str) -> tuple[str, dict]:
    p = path.split("?")[0]
    q = {}
    if "?" in path:
        for kv in path.split("?", 1)[1].split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                q[k] = v
    return p, q


def configured_box_mode() -> str:
    with LOCK:
        return sc.normalize_box_mode(STATE["config"].get("box_mode"))


def configured_pattern_family() -> str:
    with LOCK:
        return sc.normalize_pattern_family(STATE["config"].get("pattern_family"))


def attach_cache_meta(payload: dict | None, *, from_cache: bool) -> dict:
    """给看板/扫描接口补齐 items、缓存年龄与 freshness 标记。"""
    out = dict(payload or {})
    cands = out.get("candidates")
    items = out.get("items")
    if cands is None:
        cands = items or []
        out["candidates"] = cands
    if items is None:
        out["items"] = list(cands)
    age = sc.cache_age_sec(out)
    out["from_cache"] = from_cache
    out["cache_age_sec"] = None if age is None else int(age)
    out["cache_fresh"] = bool(age is not None and age < sc.SCAN_CACHE_TTL)
    out["cache_ttl_sec"] = sc.SCAN_CACHE_TTL
    return out


def scan_file_for_mode(mode: str) -> Path:
    return sc.CRYPTO_FILE if mode == "crypto" else WATCH_FILE


def expected_scope(mode: str) -> str:
    if mode == "crypto":
        return "crypto"
    if mode == "pool":
        return "pool"
    return "market"


def load_scan_cache(mode: str) -> dict | None:
    path = scan_file_for_mode(mode)
    data = read_json(path, None)
    if not data:
        return None
    scope = data.get("scope")
    if not scope:
        scope = "market" if data.get("universe_size") else "pool"
    if scope != expected_scope(mode):
        return None
    return data


def scan_cache_response(mode: str, force: bool = False) -> dict | None:
    """
    1 小时内且非 force 时返回缓存 payload（from_cache=True），否则 None。
    不调用 fetch_universe / 深度扫描。
    """
    if force:
        return None
    data = load_scan_cache(mode)
    if not data or not sc.is_fresh_scan_cache(data):
        return None
    data_box = sc.normalize_box_mode(data.get("box_mode") or "classic")
    if data_box != configured_box_mode():
        return None
    data_fam = sc.normalize_pattern_family(data.get("pattern_family") or "box")
    if data_fam != configured_pattern_family():
        return None
    out = attach_cache_meta(data, from_cache=True)
    out["status"] = "cached"
    out["msg"] = "1小时内使用缓存结果，未重新全量扫描"
    return out


def log(msg: str) -> None:
    print(msg, flush=True)
    with LOCK:
        STATE["scan_log"].append(f"{sc.now_str()}  {msg}")
        STATE["scan_log"] = STATE["scan_log"][-40:]


def snapshot_scan_progress() -> dict:
    """复制当前 scan_progress。调用方应持有 LOCK（RLock 允许嵌套）。"""
    sp = STATE.get("scan_progress") or default_scan_progress()
    try:
        pct = float(sp.get("pct") or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        done = int(sp.get("done") or 0)
    except (TypeError, ValueError):
        done = 0
    try:
        total = int(sp.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return {
        "running": bool(sp.get("running")),
        "mode": sp.get("mode"),
        "phase": str(sp.get("phase") or ""),
        "done": done,
        "total": total,
        "pct": pct,
        "message": str(sp.get("message") or ""),
        "started_at": sp.get("started_at"),
        "updated_at": sp.get("updated_at"),
    }


def _progress_pct(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(min(100.0, max(0.0, 100.0 * done / total)), 1)


def begin_scan_progress(mode: str) -> None:
    """标记扫描开始。调用方应持有 LOCK。"""
    now = sc.iso_now()
    STATE["scanning"] = True
    STATE["scan_progress"] = {
        "running": True,
        "mode": mode,
        "phase": "start",
        "done": 0,
        "total": 0,
        "pct": 0.0,
        "message": "扫描启动…",
        "started_at": now,
        "updated_at": now,
    }


def update_scan_progress(message=None, phase=None, done=None, total=None) -> None:
    """由扫描进度回调频繁写入；所有打开中的看板轮询同一份状态。"""
    with LOCK:
        if "scan_progress" not in STATE or not isinstance(STATE["scan_progress"], dict):
            STATE["scan_progress"] = default_scan_progress()
        sp = STATE["scan_progress"]
        if message is not None:
            sp["message"] = str(message)
        if phase is not None:
            sp["phase"] = str(phase)
        if done is not None:
            try:
                sp["done"] = max(0, int(done))
            except (TypeError, ValueError):
                pass
        if total is not None:
            try:
                sp["total"] = max(0, int(total))
            except (TypeError, ValueError):
                pass
        sp["pct"] = _progress_pct(int(sp.get("done") or 0), int(sp.get("total") or 0))
        sp["updated_at"] = sc.iso_now()
        if STATE["scanning"]:
            sp["running"] = True


def finish_scan_progress(ok: bool, message: str) -> None:
    with LOCK:
        STATE["scanning"] = False
        sp = STATE.get("scan_progress") or default_scan_progress()
        STATE["scan_progress"] = sp
        sp["running"] = False
        sp["phase"] = "done" if ok else "error"
        sp["message"] = message
        if ok:
            if int(sp.get("total") or 0) > 0:
                sp["done"] = int(sp["total"])
            sp["pct"] = 100.0
            STATE["last_scan"] = sc.now_str()
        sp["updated_at"] = sc.iso_now()


def reset_scan_runtime_state() -> None:
    """测试辅助：清空扫描锁与进度（不碰配置/K线缓存）。"""
    with LOCK:
        STATE["scanning"] = False
        STATE["scan_log"] = []
        STATE["last_scan"] = None
        STATE["scan_progress"] = default_scan_progress()


def join_scan_payload() -> dict:
    """已在扫描：让第二个客户端加入同一进度，不开第二轮任务。"""
    with LOCK:
        return {
            "status": "running",
            "msg": "扫描进行中",
            "scanning": True,
            "from_cache": False,
            "scan_progress": snapshot_scan_progress(),
            "scan_log": list(STATE["scan_log"][-12:]),
        }


def status_payload() -> dict:
    with LOCK:
        watch = read_json(WATCH_FILE, {}) or {}
        return {
            "scanning": STATE["scanning"],
            "scan_progress": snapshot_scan_progress(),
            "last_scan": STATE["last_scan"],
            "scan_log": list(STATE["scan_log"][-12:]),
            "as_of": watch.get("as_of"),
            "updated": watch.get("updated"),
            "cache_fresh": sc.is_fresh_scan_cache(watch),
            "is_trading_time": sc.is_trading_time(),
        }


def make_scan_progress_cb():
    """结构化进度每次更新；scan_log 节流，避免全市场每票打满日志。"""
    last_log_at = [0.0]
    last_log_done = [-1]
    last_phase = [""]

    def cb(msg: str, *, phase: str | None = None, done: int | None = None,
           total: int | None = None):
        update_scan_progress(message=msg, phase=phase, done=done, total=total)
        now = time.time()
        phase_changed = phase is not None and phase != last_phase[0]
        if phase is not None:
            last_phase[0] = phase
        should_log = False
        if done is None:
            should_log = True
        elif phase_changed:
            should_log = True
        elif total and int(done) >= int(total):
            should_log = True
        elif int(done) == 1:
            should_log = True
        elif int(done) - last_log_done[0] >= 25:
            should_log = True
        elif now - last_log_at[0] >= 2.0:
            should_log = True
        if should_log:
            log(msg)
            last_log_at[0] = now
            if done is not None:
                try:
                    last_log_done[0] = int(done)
                except (TypeError, ValueError):
                    pass

    return cb


def load_config() -> None:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            with LOCK:
                for k, v in DEFAULT_CONFIG.items():
                    if k in data:
                        STATE["config"][k] = data[k]
                STATE["config"]["scan_workers"] = sc.clamp_scan_workers(
                    STATE["config"].get("scan_workers"))
    except Exception:
        pass


def save_config(cfg: dict) -> None:
    incoming = dict(cfg)
    if "box_mode" in incoming:
        incoming["box_mode"] = sc.normalize_box_mode(incoming.get("box_mode"))
    if "pattern_family" in incoming:
        incoming["pattern_family"] = sc.normalize_pattern_family(
            incoming.get("pattern_family"))
    if "scan_workers" in incoming:
        incoming["scan_workers"] = sc.clamp_scan_workers(incoming.get("scan_workers"))
    with LOCK:
        STATE["config"].update(incoming)
        data = dict(STATE["config"])
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def launch_scan_thread(mode: str, top: int = sc.MARKET_TOP, force: bool = False) -> bool:
    """若未在扫则写入进度并启动后台线程。已在扫则返回 False。"""
    with LOCK:
        if STATE["scanning"]:
            return False
        begin_scan_progress(mode)
    threading.Thread(
        target=scan_worker,
        kwargs={"mode": mode, "top": top, "force": force},
        daemon=True,
    ).start()
    return True


def start_or_join_scan(mode: str, top: int = sc.MARKET_TOP, force: bool = False) -> dict:
    """
    POST /api/scan 核心：已在扫 → join；1h 缓存命中 → cached；否则启动一轮。
    不在此路径触发全市场 universe 拉取（缓存命中时直接返回）。
    """
    with LOCK:
        if STATE["scanning"]:
            return join_scan_payload()
    cached = scan_cache_response(mode, force=force)
    if cached:
        log("命中1小时扫描缓存，跳过全量扫描" +
            ("（币圈）" if mode == "crypto" else
             "（全市场）" if mode in ("market", "quick") else "（自选池）"))
        with LOCK:
            cached["scanning"] = STATE["scanning"]
            cached["scan_progress"] = snapshot_scan_progress()
        return cached
    with LOCK:
        if STATE["scanning"]:
            return join_scan_payload()
        begin_scan_progress(mode)
        progress = snapshot_scan_progress()
    log("手动触发扫描…" + ("（全市场全量）" if mode == "market" else
                            ("（全市场快扫）" if mode == "quick" else
                             ("（币圈）" if mode == "crypto" else "（自选池）"))) +
        (" 强制刷新" if force else ""))
    threading.Thread(
        target=scan_worker,
        kwargs={"mode": mode, "top": top, "force": force},
        daemon=True,
    ).start()
    return {
        "status": "started",
        "from_cache": False,
        "scanning": True,
        "scan_progress": progress,
    }


def scan_worker(mode: str = "pool", top: int = sc.MARKET_TOP, force: bool = False) -> None:
    """真正执行扫描并写盘。调度任务与「强制重扫」传 force=True 以刷新当日股票清单。"""
    with LOCK:
        if not STATE["scanning"]:
            begin_scan_progress(mode)
    msg = "扫描结束"
    ok = False
    cb = make_scan_progress_cb()
    workers = sc.resolve_scan_workers(None)
    try:
        if mode == "market":
            rows = sc.run_market_scan(full=True, progress=cb, workers=workers,
                                      force_universe=force)
        elif mode == "quick":
            rows = sc.run_market_scan(full=False, top=top, progress=cb, workers=workers,
                                      force_universe=force)
        elif mode == "crypto":
            rows = sc.run_crypto_scan(top=sc.CRYPTO_TOP_N, progress=cb, workers=workers)
        else:
            rows = sc.run_scan(network=True, progress=cb, workers=workers)
        msg = f"扫描完成：{len(rows)} 只，达标 {sum(1 for r in rows if r.get('qualified'))} 只"
        log(msg)
        ok = True
    except Exception as e:
        msg = f"扫描失败: {e}"
        log(msg)
    finally:
        finish_scan_progress(ok, msg)


def scheduler_loop() -> None:
    """交易日 11:30 / 15:00 自动全市场扫描（config.auto 开启时）。"""
    while True:
        try:
            with LOCK:
                auto = STATE["config"].get("auto", True)
                times = STATE["config"].get("auto_times") or []
                busy = STATE["scanning"]
            now = datetime.now(sc.BJT)
            if auto and now.weekday() < 5 and not busy:
                hm = now.strftime("%H:%M")
                for t in times:
                    key = f"{now.strftime('%Y-%m-%d')} {t}"
                    with LOCK:
                        already = key in STATE["auto_done"]
                    if hm == t and not already:
                        if launch_scan_thread("market", force=True):
                            with LOCK:
                                STATE["auto_done"].add(key)
                            log(f"自动扫描触发（{t}，强制刷新）…")
                        break
        except Exception:
            pass
        time.sleep(20)


def read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def pool_stocks() -> list[dict]:
    return read_json(POOL_FILE, {"stocks": []}).get("stocks", [])


def save_pool(stocks: list[dict]) -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    POOL_FILE.write_text(
        json.dumps({"updated": sc.now_str(), "stocks": stocks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_quotes(codes: list[str]) -> dict:
    """批量实时行情（价格/涨跌/换手/量比），2.5s 内存缓存，8 并发。"""
    out, need = {}, []
    now = time.time()
    for c in codes:
        hit = STATE["quote_cache"].get(c)
        if hit and now - hit[0] < 2.5:
            out[c] = hit[1]
        else:
            need.append(c)
    if need:
        def one(c):
            try:
                return c, sc.fetch_quote(c)
            except Exception:
                return c, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            for c, qq in ex.map(one, need):
                if qq:
                    payload = {"price": qq["price"], "chg": qq["chg"],
                               "turnover": qq["turnover"], "volume_ratio": qq["volume_ratio"]}
                else:
                    payload = None
                STATE["quote_cache"][c] = (time.time(), payload)
                out[c] = payload
    return out


def _kline_today() -> str:
    return datetime.now(sc.BJT).strftime("%Y-%m-%d")


def _kline_mem_key(market: str, code: str):
    return ("k", market, code)


def _valid_kline_payload(payload) -> bool:
    """成功日K才可缓存：拒绝 error 字段与空 bars（避免把失败当命中）。"""
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    bars = payload.get("bars")
    return isinstance(bars, list) and len(bars) > 0


def _kline_mem_get(market: str, code: str):
    with LOCK:
        cached = STATE["kline_cache"].get(_kline_mem_key(market, code))
    if not cached:
        return None
    ts, payload = cached
    if time.time() - ts >= KLINE_CACHE_TTL:
        return None
    if not _valid_kline_payload(payload):
        return None
    return payload


def _kline_mem_put(market: str, code: str, payload: dict) -> None:
    if not _valid_kline_payload(payload):
        return
    with LOCK:
        STATE["kline_cache"][_kline_mem_key(market, code)] = (time.time(), payload)


def _kline_disk_path(market: str, code: str) -> Path:
    safe_m = re.sub(r"[^a-z0-9]", "", (market or "stock").lower()) or "stock"
    safe_c = re.sub(r"[^A-Za-z0-9._-]", "_", str(code or ""))[:48] or "unknown"
    return KLINE_DISK_DIR / f"{safe_m}_{safe_c}_{_kline_today()}.json"


def _kline_disk_get(market: str, code: str):
    path = _kline_disk_path(market, code)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = float(data.get("ts") or 0)
        payload = data.get("payload")
        if time.time() - ts >= KLINE_CACHE_TTL:
            return None
        if not _valid_kline_payload(payload):
            return None
        return payload
    except Exception:
        return None


def _kline_disk_put(market: str, code: str, payload: dict) -> None:
    if not _valid_kline_payload(payload):
        return
    try:
        KLINE_DISK_DIR.mkdir(parents=True, exist_ok=True)
        path = _kline_disk_path(market, code)
        tmp = path.with_suffix(".json.tmp")
        blob = json.dumps({"ts": time.time(), "payload": payload}, ensure_ascii=False)
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(path)
        prefix = path.name.rsplit("_", 1)[0] + "_"
        for old in KLINE_DISK_DIR.glob(f"{prefix}*.json"):
            if old.resolve() != path.resolve():
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _with_box(payload: dict) -> dict:
    """按当前配置现算 box + 旗形叠加，使 K 线与卡片形态族一致；不改缓存里的 bars。"""
    if not _valid_kline_payload(payload):
        return payload
    mode = configured_box_mode()
    family = configured_pattern_family()
    out = dict(payload)
    out["box"] = sc.compute_box(out.get("bars") or [], mode=mode)
    out["box_mode"] = mode
    out["flag"] = sc.detect_high_flag(out.get("bars") or [])
    out["pattern_family"] = family
    return out


def get_kline(code: str, lmt: int = 160, market: str = "stock") -> dict | None:
    """个股/币日K + 箱体。成功结果缓存 ~45 分钟；错误与空序列不写入。"""
    hit = _kline_mem_get(market, code)
    if hit:
        return _with_box(hit)
    hit = _kline_disk_get(market, code)
    if hit:
        _kline_mem_put(market, code, hit)
        return _with_box(hit)
    try:
        if market == "crypto":
            bars = sc.fetch_crypto_kline(code)
            if not bars:
                return {"code": code, "error": "empty kline"}
            payload = {
                "code": code, "name": code, "price": bars[-1]["close"],
                "chg": None, "turnover": None, "volume_ratio": None,
                "bar_date": bars[-1]["date"], "bars": bars,
            }
        else:
            quote = sc.fetch_quote(code)
            bars = sc.fetch_kline(code, lmt=lmt)
            if not bars:
                return {"code": code, "error": "empty kline"}
            payload = {
                "code": code, "name": quote.get("name", ""),
                "price": quote["price"], "chg": quote["chg"],
                "turnover": quote["turnover"], "volume_ratio": quote["volume_ratio"],
                "bar_date": bars[-1]["date"], "bars": bars,
            }
        _kline_mem_put(market, code, payload)
        _kline_disk_put(market, code, payload)
        return _with_box(payload)
    except Exception as e:
        return {"code": code, "error": str(e)[:150]}


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeGenuis/2.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            pass
        return {}

    def do_GET(self):
        p, q = parse_query(self.path)

        if p in ("/", "/index.html"):
            try:
                self._send(200, (ROOT / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._json({"error": "dashboard.html 不存在"}, 404)
        elif p.startswith("/static/"):
            fp = (ROOT / p.lstrip("/")).resolve()
            if fp.is_relative_to(ROOT.resolve()) and fp.is_file() and fp.suffix in (
                ".css", ".woff2", ".svg", ".png", ".ico", ".js"
            ):
                ctype = {
                    ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                    ".svg": "image/svg+xml", ".png": "image/png",
                    ".ico": "image/x-icon", ".js": "text/javascript; charset=utf-8",
                }[fp.suffix]
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)
        elif p == "/api/watchlist":
            raw = read_json(WATCH_FILE, {"as_of": None, "updated": None, "candidates": [], "items": []})
            self._json(attach_cache_meta(raw, from_cache=bool(raw.get("candidates") or raw.get("items"))))
        elif p == "/api/crypto":
            raw = read_json(sc.CRYPTO_FILE, {"as_of": None, "updated": None, "candidates": [], "items": []})
            self._json(attach_cache_meta(raw, from_cache=bool(raw.get("candidates") or raw.get("items"))))
        elif p == "/api/hot":
            hot, _ = sc.fetch_hot_topics()
            self._json({"hot_topics": hot})
        elif p == "/api/pool":
            self._json({"stocks": pool_stocks()})
        elif p == "/api/status":
            self._json(status_payload())
        elif p == "/api/config":
            with LOCK:
                cfg = dict(STATE["config"])
            cfg["box_mode"] = sc.normalize_box_mode(cfg.get("box_mode"))
            cfg["pattern_family"] = sc.normalize_pattern_family(cfg.get("pattern_family"))
            cfg["scan_workers"] = sc.clamp_scan_workers(cfg.get("scan_workers"))
            cfg["box_modes"] = list(sc.BOX_MODES)
            cfg["pattern_families"] = list(sc.PATTERN_FAMILIES)
            self._json(cfg)
        elif p == "/api/quotes":
            codes = [c for c in q.get("codes", "").split(",") if c.isdigit()][:100]
            self._json(get_quotes(codes))
        elif p == "/api/kline":
            code = q.get("code", "")
            market = q.get("market", "stock")
            if not code or (market == "stock" and not code.isdigit()):
                self._json({"error": "code required"}, 400)
                return
            lmt = 160
            try:
                lmt = min(500, max(60, int(q.get("lmt", 160))))
            except ValueError:
                pass
            self._json(get_kline(code, lmt, market))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p, q = parse_query(self.path)
        if p == "/api/scan":
            body = self._body()
            mode = body.get("mode", "pool")
            if mode not in ("pool", "market", "quick", "crypto"):
                mode = "pool"
            top = int(body.get("top") or sc.MARKET_TOP)
            force = as_truthy(q.get("force")) or as_truthy(body.get("force"))
            self._json(start_or_join_scan(mode, top=top, force=force))
        elif p == "/api/config":
            body = self._body()
            save_config(body)
            log("配置已保存")
            with LOCK:
                cfg = dict(STATE["config"])
            cfg["box_modes"] = list(sc.BOX_MODES)
            cfg["pattern_families"] = list(sc.PATTERN_FAMILIES)
            cfg["box_mode"] = sc.normalize_box_mode(cfg.get("box_mode"))
            cfg["pattern_family"] = sc.normalize_pattern_family(cfg.get("pattern_family"))
            cfg["scan_workers"] = sc.clamp_scan_workers(cfg.get("scan_workers"))
            self._json({"ok": True, "config": cfg})
        elif p == "/api/pool":
            body = self._body()
            action = body.get("action", "")
            code = str(body.get("code", "")).strip()
            name = str(body.get("name", "")).strip()
            stocks = pool_stocks()
            if action == "add" and code.isdigit():
                if not any(s.get("code") == code for s in stocks):
                    if not name:
                        try:
                            name = sc.fetch_quote(code).get("name", code)
                        except Exception:
                            name = code
                    stocks.append({"code": code, "name": name, "theme": body.get("theme", "")})
                    save_pool(stocks)
                    self._json({"ok": True, "stocks": stocks})
                else:
                    self._json({"ok": True, "msg": "已在池中", "stocks": stocks})
            elif action == "remove":
                stocks = [s for s in stocks if s.get("code") != code]
                save_pool(stocks)
                self._json({"ok": True, "stocks": stocks})
            else:
                self._json({"error": "非法请求"}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="TradeGenuis 箱体突破看板服务器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8808)
    args = ap.parse_args()

    load_config()
    log(f"自动扫描：{'开' if STATE['config'].get('auto') else '关'} · "
        f"{' / '.join(STATE['config'].get('auto_times') or [])} 每个交易日")
    if not WATCH_FILE.exists():
        log("未发现 data/watchlist.json，启动后台首次全市场扫描…")
        launch_scan_thread("market")

    threading.Thread(target=scheduler_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"看板已启动: http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
