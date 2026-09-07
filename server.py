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
  POST /api/scan            触发扫描 {mode, force}；1 小时内默认返回缓存
  GET  /api/status          扫描状态/日志
  GET/POST /api/config      配置（自动扫描 / Telegram / box_mode / pattern_family）

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
    "box_mode": "classic",              # classic | p0 | p1（p1 为骨架，看板禁用）
    "pattern_family": "box",            # box | high_flag（默认 box，不打断现有用户）
}

STATE = {
    "scanning": False,
    "scan_log": [],
    "last_scan": None,
    "kline_cache": {},
    "quote_cache": {},         # code -> (ts, payload) 2.5s 内存缓存
    "auto_done": set(),        # 已触发的自动扫描时间键 "YYYY-MM-DD HH:MM"
    "config": dict(DEFAULT_CONFIG),
}
LOCK = threading.Lock()
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


def load_config() -> None:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            with LOCK:
                for k, v in DEFAULT_CONFIG.items():
                    if k in data:
                        STATE["config"][k] = data[k]
    except Exception:
        pass


def save_config(cfg: dict) -> None:
    incoming = dict(cfg)
    if "box_mode" in incoming:
        incoming["box_mode"] = sc.normalize_box_mode(incoming.get("box_mode"))
    if "pattern_family" in incoming:
        incoming["pattern_family"] = sc.normalize_pattern_family(
            incoming.get("pattern_family"))
    with LOCK:
        STATE["config"].update(incoming)
        data = dict(STATE["config"])
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def scan_worker(mode: str = "pool", top: int = sc.MARKET_TOP, force: bool = False) -> None:
    """真正执行扫描并写盘。调度任务与「强制重扫」传 force=True 以刷新当日股票清单。"""
    try:
        STATE["scanning"] = True
        if mode == "market":
            rows = sc.run_market_scan(full=True, progress=lambda m: log(m),
                                      force_universe=force)
        elif mode == "quick":
            rows = sc.run_market_scan(full=False, top=top, progress=lambda m: log(m),
                                      force_universe=force)
        elif mode == "crypto":
            rows = sc.run_crypto_scan(top=sc.CRYPTO_TOP_N, progress=lambda m: log(m))
        else:
            rows = sc.run_scan(network=True, progress=lambda m: log(m))
        log(f"扫描完成：{len(rows)} 只，达标 {sum(1 for r in rows if r.get('qualified'))} 只")
        STATE["last_scan"] = sc.now_str()
    except Exception as e:
        log(f"扫描失败: {e}")
    finally:
        STATE["scanning"] = False


def scheduler_loop() -> None:
    """交易日 11:30 / 15:00 自动全市场扫描（config.auto 开启时）。"""
    while True:
        try:
            with LOCK:
                auto = STATE["config"].get("auto", True)
                times = STATE["config"].get("auto_times") or []
            now = datetime.now(sc.BJT)
            if auto and now.weekday() < 5 and not STATE["scanning"]:
                hm = now.strftime("%H:%M")
                for t in times:
                    key = f"{now.strftime('%Y-%m-%d')} {t}"
                    if hm == t and key not in STATE["auto_done"]:
                        STATE["auto_done"].add(key)
                        log(f"自动扫描触发（{t}，强制刷新）…")
                        threading.Thread(
                            target=scan_worker,
                            kwargs={"mode": "market", "force": True},
                            daemon=True,
                        ).start()
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
            with LOCK:
                watch = read_json(WATCH_FILE, {}) or {}
                self._json({
                    "scanning": STATE["scanning"],
                    "last_scan": STATE["last_scan"],
                    "scan_log": STATE["scan_log"][-12:],
                    "as_of": watch.get("as_of"),
                    "updated": watch.get("updated"),
                    "cache_fresh": sc.is_fresh_scan_cache(watch),
                    "is_trading_time": sc.is_trading_time(),
                })
        elif p == "/api/config":
            with LOCK:
                cfg = dict(STATE["config"])
            cfg["box_mode"] = sc.normalize_box_mode(cfg.get("box_mode"))
            cfg["pattern_family"] = sc.normalize_pattern_family(cfg.get("pattern_family"))
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
            if STATE["scanning"]:
                self._json({"status": "running", "msg": "扫描进行中"})
            else:
                body = self._body()
                mode = body.get("mode", "pool")
                if mode not in ("pool", "market", "quick", "crypto"):
                    mode = "pool"
                top = int(body.get("top") or sc.MARKET_TOP)
                force = as_truthy(q.get("force")) or as_truthy(body.get("force"))
                cached = scan_cache_response(mode, force=force)
                if cached:
                    log("命中1小时扫描缓存，跳过全量扫描" +
                        ("（币圈）" if mode == "crypto" else
                         "（全市场）" if mode in ("market", "quick") else "（自选池）"))
                    self._json(cached)
                    return
                threading.Thread(
                    target=scan_worker,
                    kwargs={"mode": mode, "top": top, "force": force},
                    daemon=True,
                ).start()
                log("手动触发扫描…" + ("（全市场全量）" if mode == "market" else
                                        ("（全市场快扫）" if mode == "quick" else
                                         ("（币圈）" if mode == "crypto" else "（自选池）"))) +
                    (" 强制刷新" if force else ""))
                self._json({"status": "started", "from_cache": False})
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
        threading.Thread(target=scan_worker, args=("market",), daemon=True).start()

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
