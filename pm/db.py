from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from pm.config import REPO_ROOT
from pm.util import dumps, iso, now_utc

SCHEMA_PATH = REPO_ROOT / "database" / "schema.sql"

sqlite3.register_adapter(Decimal, lambda d: str(d))


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection, mode: str, starting_cash: Decimal | None = None) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for table, col, decl in (("markets", "game_start_time", "TEXT"), ("positions", "take_profit_price", "TEXT"), ("positions", "stop_loss_price", "TEXT")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    defaults = {
        "trading_enabled": "1" if mode == "paper" else "0",
        "kill_reason": "",
        "paused_until": "",
        "pause_reason": "",
        "last_heartbeat": "",
        "next_run_at": "",
        "run_now": "0",
        "activities_cursor": "",
        "consecutive_api_errors": "0",
        "claude_paused_until": "",
        "live_armed_at": "",
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO control(key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, iso(now_utc())),
        )
    if mode == "paper" and starting_cash is not None:
        n = conn.execute("SELECT COUNT(*) FROM paper_ledger").fetchone()[0]
        if n == 0:
            conn.execute(
                "INSERT INTO paper_ledger(ts, type, amount, ref_table, ref_id, note) VALUES (?, 'deposit', ?, NULL, NULL, 'initial paper deposit')",
                (iso(now_utc()), str(starting_cash)),
            )


def get_control(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def set_control(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO control(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, "" if value is None else str(value), iso(now_utc())),
    )


def log_event(conn: sqlite3.Connection, actor: str, level: str, event_type: str, ref_table: str | None = None, ref_id: Any = None, payload: Any = None) -> int:
    cur = conn.execute(
        "INSERT INTO events_log(ts, actor, level, event_type, ref_table, ref_id, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (iso(now_utc()), actor, level, event_type, ref_table, None if ref_id is None else str(ref_id), dumps(payload) if payload is not None else None),
    )
    return cur.lastrowid


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> int:
    cols = list(row.keys())
    sql = f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [_adapt(v) for v in row.values()])
    return cur.lastrowid


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], conflict_cols: Iterable[str]) -> None:
    cols = list(row.keys())
    conflict = list(conflict_cols)
    updates = [c for c in cols if c not in conflict]
    sql = (
        f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(conflict)}) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in updates)
    )
    conn.execute(sql, [_adapt(v) for v in row.values()])


def update(conn: sqlite3.Connection, table: str, where: dict[str, Any], values: dict[str, Any]) -> int:
    set_sql = ", ".join(f"{k} = ?" for k in values)
    where_sql = " AND ".join(f"{k} = ?" for k in where)
    cur = conn.execute(f"UPDATE {table} SET {set_sql} WHERE {where_sql}", [_adapt(v) for v in values.values()] + [_adapt(v) for v in where.values()])
    return cur.rowcount


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default=None):
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def _adapt(v: Any) -> Any:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dict, list)):
        return dumps(v)
    return v


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
