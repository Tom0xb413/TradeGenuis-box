#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场标的本地滚动 K 线库（SQLite）。

每 (symbol, interval) 最多保留 BAR_CAP（默认 180）根，按 bar 时间去重后留最新。
A 股全市场不写入本库（体量过大，见 docs/kline-store.md）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from global_pool import (
    normalize_crypto_interval,
    parse_bar_datetime,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_PATH = DATA / "kline_store.sqlite"
MAINTAINED_FILE = DATA / "maintained_pool.json"

BAR_CAP = 180
SOURCE_CHOICES = ("auto", "gate", "crypto", "sina", "naver")
DEFAULT_SOURCE = "auto"

BJT = timezone(timedelta(hours=8))

_LOCK = threading.RLock()
_SCHEMA_READY: set[str] = set()


def _now_iso() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def normalize_source(raw) -> str:
    """数据源：auto | gate | crypto | sina | naver；非法回退 auto。"""
    key = str(raw or "").strip().lower()
    if key in SOURCE_CHOICES:
        return key
    return DEFAULT_SOURCE


def bar_timestamp(bar: dict) -> int | None:
    """bars[].date → unix 秒。无法解析则 None。"""
    try:
        dt = parse_bar_datetime(bar.get("date"))
    except (TypeError, ValueError, KeyError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return int(dt.timestamp())


def normalize_bar(bar: dict) -> dict | None:
    """统一 OHLC 字典；缺字段则丢弃。"""
    if not isinstance(bar, dict):
        return None
    date = str(bar.get("date") or "").strip()
    if not date:
        return None
    try:
        o, h, low, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return None
    vol = bar.get("vol") or 0
    try:
        vol = float(vol or 0)
    except (TypeError, ValueError):
        vol = 0.0
    ts = bar_timestamp({"date": date})
    if ts is None:
        return None
    return {
        "date": date,
        "open": o, "high": h, "low": low, "close": c,
        "vol": vol,
        "ts": ts,
    }


def merge_trim_bars(existing, incoming, cap: int = BAR_CAP) -> list[dict]:
    """
    按 date 去重合并（后写覆盖先写），按时间排序，只留最近 cap 根。

    增量同步：旧序列 + 带重叠的新片段 → 完整滚动窗口。
    """
    by_date: dict[str, dict] = {}
    for src in (existing, incoming):
        for raw in src or []:
            b = normalize_bar(raw)
            if b:
                by_date[b["date"]] = b
    out = [by_date[k] for k in sorted(by_date, key=lambda d: (by_date[d]["ts"], d))]
    n = int(cap) if cap else 0
    if n and len(out) > n:
        out = out[-n:]
    return out


def _db_key(path: Path | None = None) -> str:
    return str((path or DB_PATH).resolve())


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开 SQLite（WAL）。调用方负责 close。"""
    db = path or DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    key = _db_key(db)
    if key not in _SCHEMA_READY:
        _init_schema(conn)
        _SCHEMA_READY.add(key)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            code TEXT PRIMARY KEY,
            name TEXT,
            asset_class TEXT,
            source TEXT,
            source_override TEXT,
            origin TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            ts INTEGER NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, vol REAL,
            PRIMARY KEY (symbol, interval, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_bars_sym_iv_date
            ON bars(symbol, interval, date);
        CREATE TABLE IF NOT EXISTS pull_status (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            last_start TEXT,
            last_end TEXT,
            last_success TEXT,
            last_error TEXT,
            bar_count INTEGER DEFAULT 0,
            source TEXT,
            lag_sec REAL,
            PRIMARY KEY (symbol, interval)
        );
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            ended_at TEXT,
            interval TEXT,
            n_symbols INTEGER,
            n_ok INTEGER,
            n_fail INTEGER,
            status TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS job_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()


def reset_schema_cache() -> None:
    """测试辅助：允许换临时库后再建表。"""
    _SCHEMA_READY.clear()


def job_get(key: str, default=None, path: Path | None = None):
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute("SELECT value FROM job_state WHERE key=?", (key,)).fetchone()
            if not row or row["value"] is None:
                return default
            return row["value"]
        finally:
            conn.close()


def job_set(key: str, value, path: Path | None = None) -> None:
    raw = "" if value is None else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    with _LOCK:
        conn = connect(path)
        try:
            conn.execute(
                "INSERT INTO job_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )
            conn.commit()
        finally:
            conn.close()


def job_get_json(key: str, default=None, path: Path | None = None):
    raw = job_get(key, None, path=path)
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def upsert_symbol(meta: dict, path: Path | None = None) -> None:
    """写入/更新 symbols 行。code 必填。"""
    code = str(meta.get("code") or "").strip()
    if not code:
        return
    now = _now_iso()
    with _LOCK:
        conn = connect(path)
        try:
            old = conn.execute("SELECT created_at FROM symbols WHERE code=?", (code,)).fetchone()
            created = (old["created_at"] if old else None) or now
            conn.execute(
                """
                INSERT INTO symbols(code, name, asset_class, source, source_override,
                                    origin, enabled, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    asset_class=excluded.asset_class,
                    source=excluded.source,
                    source_override=COALESCE(excluded.source_override, symbols.source_override),
                    origin=excluded.origin,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    code,
                    meta.get("name") or code,
                    meta.get("asset_class") or "",
                    meta.get("source") or "",
                    meta.get("source_override"),
                    meta.get("origin") or "default",
                    1 if meta.get("enabled", True) else 0,
                    created,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def set_source_override(code: str, source: str | None, path: Path | None = None) -> None:
    src = None if not source or normalize_source(source) == "auto" else normalize_source(source)
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute("SELECT code FROM symbols WHERE code=?", (code,)).fetchone()
            now = _now_iso()
            if row:
                conn.execute(
                    "UPDATE symbols SET source_override=?, updated_at=? WHERE code=?",
                    (src, now, code),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO symbols(code, name, asset_class, source, source_override,
                                        origin, enabled, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,1,?,?)
                    """,
                    (code, code, "", "", src, "user", now, now),
                )
            conn.commit()
        finally:
            conn.close()


def delete_symbol_data(code: str, path: Path | None = None) -> None:
    """从库中删除该标的的元数据、K 线与拉取状态。"""
    with _LOCK:
        conn = connect(path)
        try:
            conn.execute("DELETE FROM bars WHERE symbol=?", (code,))
            conn.execute("DELETE FROM pull_status WHERE symbol=?", (code,))
            conn.execute("DELETE FROM symbols WHERE code=?", (code,))
            conn.commit()
        finally:
            conn.close()


def get_bars(symbol: str, interval: str, cap: int = BAR_CAP,
             path: Path | None = None) -> list[dict]:
    iv = normalize_crypto_interval(interval)
    with _LOCK:
        conn = connect(path)
        try:
            rows = conn.execute(
                """
                SELECT date, open, high, low, close, vol, ts
                FROM bars WHERE symbol=? AND interval=?
                ORDER BY ts ASC
                """,
                (symbol, iv),
            ).fetchall()
        finally:
            conn.close()
    bars = [{
        "date": r["date"], "open": r["open"], "high": r["high"],
        "low": r["low"], "close": r["close"], "vol": r["vol"] or 0,
        "ts": r["ts"],
    } for r in rows]
    if cap and len(bars) > cap:
        bars = bars[-int(cap):]
    return bars


def last_bar_ts(symbol: str, interval: str, path: Path | None = None) -> int | None:
    iv = normalize_crypto_interval(interval)
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT MAX(ts) AS m FROM bars WHERE symbol=? AND interval=?",
                (symbol, iv),
            ).fetchone()
            if not row or row["m"] is None:
                return None
            return int(row["m"])
        finally:
            conn.close()


def bar_count(symbol: str, interval: str, path: Path | None = None) -> int:
    iv = normalize_crypto_interval(interval)
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM bars WHERE symbol=? AND interval=?",
                (symbol, iv),
            ).fetchone()
            return int(row["n"] if row else 0)
        finally:
            conn.close()


def replace_bars(symbol: str, interval: str, bars: list[dict],
                  cap: int = BAR_CAP, path: Path | None = None) -> list[dict]:
    """用 merge_trim 后的完整窗口替换该 (symbol, interval) 的 K 线。"""
    iv = normalize_crypto_interval(interval)
    old = get_bars(symbol, iv, cap=0, path=path)
    merged = merge_trim_bars(old, bars, cap=cap)
    with _LOCK:
        conn = connect(path)
        try:
            conn.execute("DELETE FROM bars WHERE symbol=? AND interval=?", (symbol, iv))
            conn.executemany(
                """
                INSERT INTO bars(symbol, interval, ts, date, open, high, low, close, vol)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                [(
                    symbol, iv, b["ts"], b["date"],
                    b["open"], b["high"], b["low"], b["close"], b["vol"],
                ) for b in merged],
            )
            conn.commit()
        finally:
            conn.close()
    return merged


def set_pull_status(symbol: str, interval: str, **fields) -> None:
    """更新 pull_status；未提供的字段保留旧值。"""
    iv = normalize_crypto_interval(interval)
    path = fields.pop("path", None)
    with _LOCK:
        conn = connect(path)
        try:
            old = conn.execute(
                "SELECT * FROM pull_status WHERE symbol=? AND interval=?",
                (symbol, iv),
            ).fetchone()
            cur = dict(old) if old else {
                "symbol": symbol, "interval": iv,
                "last_start": None, "last_end": None, "last_success": None,
                "last_error": None, "bar_count": 0, "source": None, "lag_sec": None,
            }
            for k, v in fields.items():
                if k in cur or k in ("last_start", "last_end", "last_success",
                                       "last_error", "bar_count", "source", "lag_sec"):
                    cur[k] = v
            conn.execute(
                """
                INSERT INTO pull_status(symbol, interval, last_start, last_end,
                    last_success, last_error, bar_count, source, lag_sec)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, interval) DO UPDATE SET
                    last_start=excluded.last_start,
                    last_end=excluded.last_end,
                    last_success=excluded.last_success,
                    last_error=excluded.last_error,
                    bar_count=excluded.bar_count,
                    source=excluded.source,
                    lag_sec=excluded.lag_sec
                """,
                (
                    symbol, iv, cur.get("last_start"), cur.get("last_end"),
                    cur.get("last_success"), cur.get("last_error"),
                    int(cur.get("bar_count") or 0), cur.get("source"),
                    cur.get("lag_sec"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def add_analysis_run(record: dict, path: Path | None = None) -> int:
    with _LOCK:
        conn = connect(path)
        try:
            cur = conn.execute(
                """
                INSERT INTO analysis_runs(started_at, ended_at, interval,
                    n_symbols, n_ok, n_fail, status, error)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record.get("started_at"), record.get("ended_at"),
                    record.get("interval"), int(record.get("n_symbols") or 0),
                    int(record.get("n_ok") or 0), int(record.get("n_fail") or 0),
                    record.get("status") or "", record.get("error") or "",
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def last_analysis_run(path: Path | None = None) -> dict | None:
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def get_symbol_row(code: str, path: Path | None = None) -> dict | None:
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute("SELECT * FROM symbols WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_pull_status(interval: str, path: Path | None = None) -> list[dict]:
    iv = normalize_crypto_interval(interval)
    with _LOCK:
        conn = connect(path)
        try:
            rows = conn.execute(
                "SELECT * FROM pull_status WHERE interval=?", (iv,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def load_maintained(path: Path | None = None) -> dict:
    """
    读取 data/maintained_pool.json。
    {default_source, sources:{code:source}}；损坏/缺失视为空。
    标的名单仍以 global_override_pool.json 为准。
    """
    fp = path or MAINTAINED_FILE
    try:
        if not fp.is_file():
            return {"default_source": DEFAULT_SOURCE, "sources": {}}
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"default_source": DEFAULT_SOURCE, "sources": {}}
        sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
        cleaned = {}
        for k, v in sources.items():
            code = str(k or "").strip()
            if code:
                cleaned[code] = normalize_source(v)
        return {
            "default_source": normalize_source(data.get("default_source")),
            "sources": cleaned,
        }
    except Exception:
        return {"default_source": DEFAULT_SOURCE, "sources": {}}


def save_maintained(default_source: str = DEFAULT_SOURCE, sources: dict | None = None,
                    path: Path | None = None) -> dict:
    fp = path or MAINTAINED_FILE
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "default_source": normalize_source(default_source),
        "sources": {str(k): normalize_source(v) for k, v in (sources or {}).items() if str(k).strip()},
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def effective_source(code: str, ident_source: str | None = None,
                     maintained: dict | None = None, path: Path | None = None) -> str:
    """单票数据源：行覆盖 > 全局 default_source > ident 推断。auto 表示走 fetch_global_instrument 默认优先级。"""
    maint = maintained if maintained is not None else load_maintained()
    row = get_symbol_row(code, path=path) or {}
    for cand in (row.get("source_override"), (maint.get("sources") or {}).get(code)):
        if cand and normalize_source(cand) != "auto":
            return normalize_source(cand)
    glob = normalize_source(maint.get("default_source"))
    if glob != "auto":
        return glob
    return ident_source or "auto"


def incremental_lookback(last_ts: int | None, interval: str, cap: int = BAR_CAP,
                         overlap: int = 3) -> int:
    """空库回填 cap 根；否则按距上次 bar 的间隔估算根数 + overlap，钳制到 [overlap+1, cap]。"""
    from global_pool import interval_hours
    cap_n = max(1, int(cap or BAR_CAP))
    if not last_ts:
        return cap_n
    hours = max(1, interval_hours(interval))
    elapsed_h = max(0.0, (datetime.now(timezone.utc).timestamp() - float(last_ts)) / 3600.0)
    need = int(elapsed_h / hours) + int(overlap)
    return max(int(overlap) + 1, min(cap_n, need))


def lag_seconds(last_ts: int | None) -> float | None:
    if not last_ts:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - float(last_ts))


def bars_as_of(interval: str, path: Path | None = None) -> str | None:
    """该周期库内最新一根 K 的 date。"""
    iv = normalize_crypto_interval(interval)
    with _LOCK:
        conn = connect(path)
        try:
            row = conn.execute(
                "SELECT date FROM bars WHERE interval=? ORDER BY ts DESC LIMIT 1",
                (iv,),
            ).fetchone()
            return row["date"] if row else None
        finally:
            conn.close()


def overall_status(interval: str, path: Path | None = None) -> dict:
    """汇总拉取状态，供 GET /api/kline_store/status。"""
    iv = normalize_crypto_interval(interval)
    rows = list_pull_status(iv, path=path)
    errors = [r for r in rows if r.get("last_error")]
    ok = [r for r in rows if r.get("last_success") and not r.get("last_error")]
    last_end = job_get("last_sync_end", None, path=path)
    last_start = job_get("last_sync_start", None, path=path)
    last_analyze = job_get("last_analyze_end", None, path=path)
    return {
        "interval": iv,
        "bar_cap": BAR_CAP,
        "last_sync_start": last_start,
        "last_sync_end": last_end,
        "last_analyze_end": last_analyze,
        "bars_as_of": bars_as_of(iv, path=path),
        "analysis_as_of": last_analyze,
        "symbol_count": len(rows),
        "ok_count": len(ok),
        "error_count": len(errors),
        "last_error": job_get("last_error", "", path=path) or "",
        "symbols": rows,
    }
