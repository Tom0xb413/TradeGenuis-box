#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场标的后台增量同步 + 本地分析。

不拉 A 股全市场进 180 根库。看板主路径只读 data/crypto.json；
本模块由 server 守护线程 / 配置 UI「立即同步/立即分析」调用。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scanner as sc
import kline_store as ks
import global_pool as gp

BJT = timezone(timedelta(hours=8))
DEFAULT_SYNC_HOURS = 4

# 进程内作业状态（供配置 UI running 标志；与 server.STATE.scanning 同时使用）
_JOB = {
    "running": False,
    "kind": None,  # sync | analyze | both
}
_JOB_LOCK = threading.Lock()


class _job_guard:
    """标记后台作业 running，可嵌套（内层不把 running 清掉）。"""

    def __init__(self, kind: str):
        self.kind = kind
        self._outer = False

    def __enter__(self):
        with _JOB_LOCK:
            self._outer = not _JOB["running"]
            _JOB["running"] = True
            _JOB["kind"] = self.kind
        return self

    def __exit__(self, *exc):
        if self._outer:
            with _JOB_LOCK:
                _JOB["running"] = False
                _JOB["kind"] = None
        return False


def with_job(kind: str):
    def deco(fn):
        def inner(*args, **kwargs):
            with _job_guard(kind):
                return fn(*args, **kwargs)
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return deco


def clamp_sync_hours(raw) -> int:
    """kline_sync_hours：1–24，默认 4。"""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SYNC_HOURS
    return max(1, min(24, v))


def parse_iso(raw) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1] + "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BJT)
        return dt
    except ValueError:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
        except ValueError:
            return None


def next_due_iso(last_end, hours: int) -> str | None:
    dt = parse_iso(last_end)
    if dt is None:
        return None
    nxt = dt + timedelta(hours=clamp_sync_hours(hours))
    return nxt.isoformat(timespec="seconds")


def sync_is_due(hours: int = DEFAULT_SYNC_HOURS, path: Path | None = None) -> bool:
    last = ks.job_get("last_sync_end", None, path=path)
    dt = parse_iso(last)
    if dt is None:
        return True
    age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    return age.total_seconds() >= clamp_sync_hours(hours) * 3600


def job_running() -> bool:
    with _JOB_LOCK:
        return bool(_JOB["running"])


def preview_default_pool(path: Path | None = None) -> list[dict]:
    """
    默认宇宙预览：优先上次同步写入的 ticker 快照（含 TOP20 币），
    否则静态侧（黄金占位 + 美股/美指/日韩），不访问网络。
    """
    tickers = ks.job_get_json("ticker_snapshot", [], path=path) or []
    gold = ks.job_get_json("gold_snapshot", None, path=path)
    if not gold:
        gold = {
            "code": gp.GOLD_CRYPTO_SYMBOLS[0], "name": gp.GOLD_DISPLAY_NAME,
            "asset_class": "gold", "market": "crypto", "source": "crypto",
        }
    return gp.build_global_pool(tickers, top_n=gp.CRYPTO_TOP_N, gold=gold, override_symbols=None)


def resolve_pool(override: list[str] | None = None, path: Path | None = None) -> tuple[list[dict], str]:
    """
    后台维护池：覆盖非空只用覆盖；否则默认混合宇宙。
    返回 (pool, mode) mode=override|default。
    """
    ov = override
    if ov is None:
        ov = gp.load_override_symbols()
    if ov:
        tickers = ks.job_get_json("ticker_snapshot", [], path=path) or []
        gold = ks.job_get_json("gold_snapshot", None, path=path)
        pool = gp.build_global_pool(tickers, gold=gold, override_symbols=ov)
        return pool, "override"
    snap = ks.job_get_json("universe_snapshot", None, path=path)
    if isinstance(snap, list) and snap:
        return snap, "default"
    return preview_default_pool(path=path), "default"


def pool_item_source(item: dict, maintained: dict | None = None, path: Path | None = None) -> str:
    code = item.get("code") or ""
    return ks.effective_source(code, item.get("source"), maintained=maintained, path=path)


def _force_arg(src: str) -> str | None:
    s = ks.normalize_source(src)
    return None if s == "auto" else s


def fetch_and_store(item: dict, interval: str, *,
                    cap: int = ks.BAR_CAP, path: Path | None = None,
                    maintained: dict | None = None) -> dict:
    """
    增量拉取一只：空库回填 cap 根，否则从 last_ts 起小窗口 + 重叠，合并去重后裁到 cap。
    返回 {ok, code, bars, error, source, lookback}。
    """
    code = str(item.get("code") or "").strip()
    iv = gp.normalize_crypto_interval(interval)
    src = pool_item_source(item, maintained=maintained, path=path)
    started = datetime.now(BJT).isoformat(timespec="seconds")
    ks.upsert_symbol({
        "code": code,
        "name": item.get("name") or code,
        "asset_class": item.get("asset_class") or "",
        "source": item.get("source") or src,
        "origin": "user" if item.get("origin") == "user" else "default",
    }, path=path)
    last_ts = ks.last_bar_ts(code, iv, path=path)
    lookback = ks.incremental_lookback(last_ts, iv, cap=cap)
    ks.set_pull_status(code, iv, last_start=started, last_error=None, path=path)
    err = ""
    inst = None
    try:
        inst = sc.fetch_global_instrument(
            code, interval=iv, lookback=lookback,
            hint_source=item.get("source"),
            hint_class=item.get("asset_class"),
            force_source=_force_arg(src),
        )
    except Exception as e:
        err = str(e)[:150]
        inst = None
    incoming = list((inst or {}).get("bars") or [])
    if not incoming:
        n = ks.bar_count(code, iv, path=path)
        msg = err or "empty kline"
        ks.set_pull_status(
            code, iv, last_end=sc.iso_now(), last_error=msg,
            bar_count=n, source=src, lag_sec=ks.lag_seconds(last_ts), path=path,
        )
        return {"ok": False, "code": code, "bars": [], "error": msg, "source": src,
                "lookback": lookback}
    used_src = (inst or {}).get("source") or src
    merged = ks.replace_bars(code, iv, incoming, cap=cap, path=path)
    last = merged[-1]["ts"] if merged else None
    ks.set_pull_status(
        code, iv,
        last_end=sc.iso_now(), last_success=sc.iso_now(), last_error="",
        bar_count=len(merged), source=used_src,
        lag_sec=ks.lag_seconds(last), path=path,
    )
    if inst:
        ks.upsert_symbol({
            "code": code,
            "name": inst.get("name") or item.get("name") or code,
            "asset_class": inst.get("asset_class") or item.get("asset_class") or "",
            "source": used_src,
            "origin": "user" if item.get("origin") == "user" else "default",
        }, path=path)
    return {"ok": True, "code": code, "bars": merged, "error": "", "source": used_src,
            "lookback": lookback, "inst": inst}


@with_job("sync")
def sync_symbols(symbols: list[str] | None = None, interval: str | None = None,
                  workers: int | None = None, progress=None,
                  retry_failed: bool = False, path: Path | None = None,
                  cap: int = ks.BAR_CAP) -> dict:
    """
    增量同步配置池（或指定 symbols）。空库回填 180；否则 last_ts→now 重叠合并。
    不访问 A 股全市场名单。
    """
    iv = gp.normalize_crypto_interval(interval or sc.load_crypto_interval())
    n_workers = sc.resolve_scan_workers(workers)
    ks.job_set("last_sync_start", sc.iso_now(), path=path)
    ks.job_set("last_error", "", path=path)
    sc.emit_progress(progress, "拉取 USDT 永续 24h 行情（组装默认宇宙）…",
                     phase="universe", done=0, total=0)

    override = gp.load_override_symbols()
    tickers: list[dict] = []
    try:
        tickers = sc.fetch_crypto_tickers()
    except Exception as e:
        sc.emit_progress(progress, f"永续行情失败，沿用快照/覆盖池：{str(e)[:80]}",
                         phase="universe", done=0, total=0)
        tickers = ks.job_get_json("ticker_snapshot", [], path=path) or []
    if tickers:
        ks.job_set("ticker_snapshot", tickers[:80], path=path)

    gold = None
    try:
        gold = sc.resolve_gold_instrument(tickers, interval=iv)
    except Exception:
        gold = ks.job_get_json("gold_snapshot", None, path=path)
    if gold:
        ks.job_set("gold_snapshot", {
            "code": gold.get("code"), "name": gold.get("name") or gp.GOLD_DISPLAY_NAME,
            "asset_class": "gold", "market": "crypto", "source": gold.get("source") or "crypto",
            "price": gold.get("price"), "chg": gold.get("chg"),
        }, path=path)

    if symbols:
        pool = gp.build_override_pool(symbols, crypto_tickers=tickers, gold=gold)
        mode = "subset"
    else:
        pool, mode = resolve_pool(override or None, path=path)
        if mode == "default":
            pool = gp.build_global_pool(tickers, top_n=gp.CRYPTO_TOP_N, gold=gold, override_symbols=None)
            ks.job_set("universe_snapshot", pool, path=path)

    if retry_failed:
        failed = {
            r["symbol"] for r in ks.list_pull_status(iv, path=path)
            if r.get("last_error")
        }
        if failed:
            pool = [x for x in pool if x.get("code") in failed]
            sc.emit_progress(progress, f"仅重试失败 {len(pool)} 只",
                             phase="sync", done=0, total=len(pool))

    maint = ks.load_maintained()
    n_pool = len(pool)
    sc.emit_progress(progress, f"{mode}池 {n_pool} 只 · 周期 {iv} · 滚动 {cap} 根（{n_workers} 线程）…",
                     phase="sync", done=0, total=n_pool)

    results, done, n_err = [], 0, 0
    lock = threading.Lock()

    def one(item: dict):
        nonlocal done, n_err
        try:
            r = fetch_and_store(item, iv, cap=cap, path=path, maintained=maint)
        except Exception as e:
            r = {"ok": False, "code": item.get("code"), "error": str(e)[:150], "bars": []}
        with lock:
            done += 1
            if not r.get("ok"):
                n_err += 1
            n_done = done
        sc.emit_progress(progress, f"同步 K 线 {n_done}/{n_pool}  {item.get('code')}",
                         phase="sync", done=n_done, total=n_pool)
        return r

    if n_pool:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(one, t) for t in pool]
            for f in as_completed(futs):
                try:
                    results.append(f.result())
                except Exception:
                    n_err += 1

    ended = sc.iso_now()
    ks.job_set("last_sync_end", ended, path=path)
    if n_err:
        ks.job_set("last_error", f"{n_err} 只拉取失败", path=path)
    ok_n = sum(1 for r in results if r.get("ok"))
    sc.emit_progress(progress, f"K 线同步完成：成功 {ok_n} / 失败 {n_err}，周期 {iv}",
                     phase="save", done=n_pool, total=n_pool)
    return {
        "ok": n_err == 0,
        "interval": iv,
        "pool_mode": mode,
        "synced": n_pool,
        "ok_count": ok_n,
        "error_count": n_err,
        "ended_at": ended,
    }


@with_job("analyze")
def analyze_from_store(interval: str | None = None, workers: int | None = None,
                       progress=None, path: Path | None = None,
                       crypto_file: Path | None = None) -> dict:
    """
    用本地库 K 线跑 analyze_crypto / 箱体 / 旗形 / 趋势线，写入 data/crypto.json。
    不访问 Gate。bars 不足 40 根则跳过。
    """
    iv = gp.normalize_crypto_interval(interval or sc.load_crypto_interval())
    n_workers = sc.resolve_scan_workers(workers)
    box_mode = sc.load_box_mode()
    pattern_family = sc.load_pattern_family()
    override = gp.load_override_symbols()
    pool, mode = resolve_pool(override or None, path=path)
    started = sc.iso_now()
    ks.job_set("last_analyze_start", started, path=path)
    n_pool = len(pool)
    sc.emit_progress(progress, f"本地分析 {n_pool} 只 · 周期 {iv} · {pattern_family}",
                     phase="analyze", done=0, total=n_pool)

    rows, done, skipped = [], 0, 0
    lock = threading.Lock()
    bars_dates: list[str] = []

    def one(item: dict):
        nonlocal done, skipped
        code = item.get("code") or ""
        try:
            bars = ks.get_bars(code, iv, cap=ks.BAR_CAP, path=path)
            meta = ks.get_symbol_row(code, path=path) or {}
            if len(bars) < 40:
                with lock:
                    skipped += 1
                return None
            px = bars[-1]["close"]
            chg = None
            if len(bars) >= 2 and bars[-2]["close"]:
                chg = (bars[-1]["close"] - bars[-2]["close"]) / bars[-2]["close"] * 100.0
            name = item.get("name") or meta.get("name") or code
            if item.get("asset_class") == "gold" or meta.get("asset_class") == "gold":
                name = gp.GOLD_DISPLAY_NAME
            ident = gp.resolve_symbol(code) or {}
            row = sc.analyze_crypto(
                code, float(px or 0), float(chg or 0), bars, box_mode=box_mode,
                name=name,
                asset_class=item.get("asset_class") or meta.get("asset_class") or "crypto",
                source=meta.get("source") or item.get("source") or "store",
                tokenized=bool(item.get("tokenized") or ident.get("tokenized")),
                gate_contract=item.get("gate_contract") or ident.get("gate_contract"),
                token_role=item.get("token_role") or ident.get("token_role"),
            )
            if row is None:
                with lock:
                    skipped += 1
                return None
            with lock:
                bars_dates.append(bars[-1]["date"])
            return row
        except Exception:
            with lock:
                skipped += 1
            return None
        finally:
            with lock:
                done += 1
                n_done = done
            sc.emit_progress(progress, f"本地分析 {n_done}/{n_pool}",
                             phase="analyze", done=n_done, total=n_pool)

    if n_pool:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(one, t) for t in pool]
            for f in as_completed(futs):
                try:
                    r = f.result()
                    if r:
                        rows.append(r)
                except Exception:
                    pass

    rows.sort(key=lambda r: (r.get("score") or 0, r.get("chg") or 0), reverse=True)

    def _count(cls: str) -> int:
        return sum(1 for r in rows if r.get("asset_class") == cls)

    gold_snap = ks.job_get_json("gold_snapshot", None, path=path)
    ended = sc.iso_now()
    as_of_bars = max(bars_dates) if bars_dates else ks.bars_as_of(iv, path=path)
    payload = sc.decorate_scan_payload({
        "as_of": sc.now_str(),
        "strategy": "箱体突破战法",
        "scope": "crypto",
        "universe_size": n_pool,
        "screened": n_pool,
        "scored": len(rows),
        "scanned": n_pool,
        "skipped": skipped,
        "done": True,
        "from_store": True,
        "box_mode": box_mode,
        "pattern_family": pattern_family,
        "crypto_interval": iv,
        "pool_mode": mode,
        "override_symbols": list(override or []),
        "override_fingerprint": gp.override_fingerprint(override),
        "hot_topics": [],
        "candidates": rows,
        "source": "kline_store",
        "analysis_as_of": ended,
        "bars_as_of": as_of_bars,
        "gold": ({"code": gold_snap.get("code"), "source": gold_snap.get("source")}
                 if gold_snap and gold_snap.get("code") else None),
        "pool_counts": {
            "crypto": _count("crypto"),
            "gold": _count("gold"),
            "us_index": _count("us_index"),
            "us_stock": _count("us_stock"),
            "jp_index": _count("jp_index"),
            "jp_stock": _count("jp_stock"),
            "kr_index": _count("kr_index"),
            "kr_stock": _count("kr_stock"),
        },
    })
    out_file = crypto_file or sc.CRYPTO_FILE
    out_file.parent.mkdir(parents=True, exist_ok=True)
    import json
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ks.job_set("last_analyze_end", ended, path=path)
    ks.add_analysis_run({
        "started_at": started, "ended_at": ended, "interval": iv,
        "n_symbols": n_pool, "n_ok": len(rows), "n_fail": skipped,
        "status": "ok", "error": "",
    }, path=path)
    sc.emit_progress(progress, f"本地分析完成：有效 {len(rows)} 只（跳过 {skipped}），"
                     f"达标 {sum(1 for r in rows if r.get('qualified'))} 只",
                     phase="save", done=n_pool, total=n_pool)
    return payload


@with_job("both")
def run_sync_and_analyze(interval: str | None = None, workers: int | None = None,
                         progress=None, symbols=None, retry_failed: bool = False,
                         path: Path | None = None, skip_analyze: bool = False) -> dict:
    """先增量同步再分析。供 4h 调度与「立即同步」。"""
    sync_res = sync_symbols(
        symbols=symbols, interval=interval, workers=workers,
        progress=progress, retry_failed=retry_failed, path=path,
    )
    payload = None
    if not skip_analyze:
        payload = analyze_from_store(
            interval=interval or sync_res.get("interval"),
            workers=workers, progress=progress, path=path,
        )
    return {"sync": sync_res, "analysis": payload}


def apply_pool_action(action: str, symbols=None, source: str | None = None,
                      default_source: str | None = None,
                      code: str | None = None, path: Path | None = None) -> dict:
    """
    配置池增删改源。删除/新增在默认宇宙下会把当前预览固化进覆盖文件。
    返回 status_payload 风格的 override 摘要 + rejected。
    """
    action = str(action or "").lower().strip()
    maint = ks.load_maintained()
    sources = dict(maint.get("sources") or {})
    glob = ks.normalize_source(default_source) if default_source is not None else maint.get("default_source")
    rejected: list[dict] = []

    raw_list: list[str] = []
    if isinstance(symbols, str):
        raw_list = gp.parse_symbol_text(symbols)
    elif isinstance(symbols, list):
        raw_list = [str(x).strip() for x in symbols if str(x).strip()]
    if code and not raw_list:
        raw_list = [str(code).strip()]

    def materialize_codes() -> list[str]:
        ov = gp.load_override_symbols()
        if ov:
            return list(ov)
        pool, _mode = resolve_pool(path=path)
        return [x.get("code") for x in pool if x.get("code")]

    if action == "clear":
        gp.clear_override_symbols()
        ks.save_maintained(glob or "auto", {}, path=None)
        return {"ok": True, "saved": [], "rejected": [], "cleared": True}

    if action in ("set_default_source", "default_source"):
        ks.save_maintained(glob or "auto", sources)
        return {"ok": True, "saved": [], "rejected": []}

    if action in ("add", "update", "bulk_source", "delete", "remove", "set"):
        if action == "set":
            result = gp.validate_symbols(raw_list, crypto_ok=sc.crypto_symbol_is_listed,
                                         gate_ok=sc.gate_contract_is_listed)
            ok_codes = [x["code"] for x in result["ok"]]
            rejected = result["bad"]
            gp.save_override_symbols(ok_codes)
            if source:
                src = ks.normalize_source(source)
                for c in ok_codes:
                    if src != "auto":
                        sources[c] = src
                    else:
                        sources.pop(c, None)
                    ks.set_source_override(c, None if src == "auto" else src, path=path)
            ks.save_maintained(glob or "auto", sources)
            return {"ok": True, "saved": result["ok"], "rejected": rejected}

        if action in ("add", "update", "bulk_source"):
            result = gp.validate_symbols(raw_list, crypto_ok=sc.crypto_symbol_is_listed,
                                         gate_ok=sc.gate_contract_is_listed)
            ok_codes = [x["code"] for x in result["ok"]]
            rejected = result["bad"]
            if action == "add" and ok_codes:
                cur = materialize_codes()
                seen = {c.upper() for c in cur}
                for c in ok_codes:
                    if c.upper() not in seen:
                        cur.append(c)
                        seen.add(c.upper())
                gp.save_override_symbols(cur)
            src = ks.normalize_source(source) if source is not None else None
            targets = ok_codes if action != "update" else ok_codes
            if action == "update" and code:
                targets = [gp.resolve_symbol(code)["code"] if gp.resolve_symbol(code) else code]
            if src is not None:
                for c in targets:
                    if src == "auto":
                        sources.pop(c, None)
                        ks.set_source_override(c, None, path=path)
                    else:
                        sources[c] = src
                        ks.set_source_override(c, src, path=path)
            ks.save_maintained(glob or "auto", sources)
            return {"ok": True, "saved": result.get("ok") if action != "update" else targets,
                    "rejected": rejected}

        if action in ("delete", "remove"):
            cur = materialize_codes()
            drop = set()
            for raw in raw_list:
                ident = gp.resolve_symbol(raw)
                drop.add((ident["code"] if ident else raw).upper())
            kept = [c for c in cur if c.upper() not in drop]
            gp.save_override_symbols(kept)
            for raw in raw_list:
                ident = gp.resolve_symbol(raw)
                c = ident["code"] if ident else raw
                sources.pop(c, None)
                ks.delete_symbol_data(c, path=path)
            ks.save_maintained(glob or "auto", sources)
            return {"ok": True, "saved": kept, "rejected": []}

    return {"ok": False, "error": "未知 action", "rejected": []}


def status_payload(interval: str | None = None, hours: int = DEFAULT_SYNC_HOURS,
                   path: Path | None = None) -> dict:
    """配置 UI 总览 + 每票拉取状态。"""
    iv = gp.normalize_crypto_interval(interval or sc.load_crypto_interval())
    hours = clamp_sync_hours(hours)
    pool, mode = resolve_pool(path=path)
    ov = gp.load_override_symbols()
    maint = ks.load_maintained()
    overall = ks.overall_status(iv, path=path)
    pull_by = {r["symbol"]: r for r in overall.get("symbols") or []}
    rows = []
    for item in pool:
        code = item.get("code") or ""
        st = pull_by.get(code) or {}
        src = pool_item_source(item, maintained=maint, path=path)
        n = int(st.get("bar_count") or 0) or ks.bar_count(code, iv, path=path)
        last_ts = ks.last_bar_ts(code, iv, path=path)
        err = st.get("last_error") or ""
        if err:
            status = "error"
        elif n <= 0:
            status = "pending"
        else:
            status = "ok"
        ident = gp.resolve_symbol(code) or {}
        rows.append({
            "code": code,
            "name": item.get("name") or ident.get("name") or code,
            "asset_class": item.get("asset_class") or ident.get("asset_class") or "",
            "source": src,
            "source_override": (ks.get_symbol_row(code, path=path) or {}).get("source_override")
            or (maint.get("sources") or {}).get(code),
            "bars": n,
            "last_pull": st.get("last_success") or st.get("last_end"),
            "last_success": st.get("last_success"),
            "lag_sec": st.get("lag_sec") if st.get("lag_sec") is not None else ks.lag_seconds(last_ts),
            "error": err,
            "status": status,
            "tokenized": bool(item.get("tokenized") or ident.get("tokenized")),
            "gate_contract": item.get("gate_contract") or ident.get("gate_contract"),
        })
    last_end = overall.get("last_sync_end")
    with _JOB_LOCK:
        running = bool(_JOB["running"])
        kind = _JOB["kind"]
    return {
        "running": running,
        "job": kind,
        "interval": iv,
        "kline_sync_hours": hours,
        "bar_cap": ks.BAR_CAP,
        "pool_mode": mode,
        "pool_count": len(rows),
        "override_count": len(ov),
        "default_source": maint.get("default_source") or "auto",
        "source_choices": list(ks.SOURCE_CHOICES),
        "last_sync_start": overall.get("last_sync_start"),
        "last_sync_end": last_end,
        "next_due": next_due_iso(last_end, hours),
        "last_analyze_end": overall.get("last_analyze_end"),
        "analysis_as_of": overall.get("analysis_as_of"),
        "bars_as_of": overall.get("bars_as_of"),
        "error_count": overall.get("error_count") or 0,
        "ok_count": overall.get("ok_count") or 0,
        "last_error": overall.get("last_error") or "",
        "symbols": rows,
        "override": {
            "symbols": ov,
            "count": len(ov),
            "mode": "override" if ov else "default",
            "fingerprint": gp.override_fingerprint(ov),
        },
    }
