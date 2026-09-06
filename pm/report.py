from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pm.db import all_rows, one, scalar, upsert
from pm.util import D, dstr, iso, now_utc


def brier_scores(conn: sqlite3.Connection) -> dict:
    rows = all_rows(conn, """
        SELECT n.slug, n.p_yes, n.price_at_research, m.settlement_price
        FROM research_notes n
        JOIN markets m ON m.slug = n.slug
        WHERE m.settlement_price IS NOT NULL AND n.p_yes IS NOT NULL
          AND n.note_id = (SELECT MAX(note_id) FROM research_notes n2 WHERE n2.slug = n.slug AND n2.created_at <= COALESCE(m.settled_at, n.created_at))
    """)
    n = 0
    bm = Decimal(0)
    bk = Decimal(0)
    for r in rows:
        outcome = Decimal(1) if (D(r["settlement_price"]) or Decimal(0)) >= Decimal("0.5") else Decimal(0)
        pm = D(r["p_yes"])
        pk = D(r["price_at_research"])
        if pm is None:
            continue
        n += 1
        bm += (pm - outcome) ** 2
        if pk is not None:
            bk += (pk - outcome) ** 2
    return {"n": n, "brier_model": (bm / n) if n else None, "brier_market": (bk / n) if n else None}


def compute_daily_stats(conn: sqlite3.Connection, day: datetime | None = None) -> dict:
    day = day or (now_utc() - timedelta(days=1))
    ds = day.strftime("%Y-%m-%d")
    start, end = f"{ds}T00:00:00Z", f"{ds}T23:59:59Z"
    first = one(conn, "SELECT equity FROM balance_snapshots WHERE ts BETWEEN ? AND ? ORDER BY snapshot_id ASC LIMIT 1", (start, end))
    last = one(conn, "SELECT equity FROM balance_snapshots WHERE ts BETWEEN ? AND ? ORDER BY snapshot_id DESC LIMIT 1", (start, end))
    prev = one(conn, "SELECT equity FROM balance_snapshots WHERE ts < ? ORDER BY snapshot_id DESC LIMIT 1", (start,))
    start_eq = D(prev["equity"]) if prev else (D(first["equity"]) if first else None)
    end_eq = D(last["equity"]) if last else start_eq
    n_orders = int(scalar(conn, "SELECT COUNT(*) FROM orders WHERE created_at BETWEEN ? AND ? AND status != 'rejected'", (start, end), 0))
    n_fills = int(scalar(conn, "SELECT COUNT(*) FROM fills WHERE ts BETWEEN ? AND ?", (start, end), 0))
    n_settled = int(scalar(conn, "SELECT COUNT(*) FROM settlements WHERE detected_at BETWEEN ? AND ?", (start, end), 0))
    n_won = int(scalar(conn, "SELECT COUNT(*) FROM settlements WHERE detected_at BETWEEN ? AND ? AND won = 1", (start, end), 0))
    fees = D(scalar(conn, "SELECT COALESCE(SUM(CAST(fee AS REAL)), 0) FROM fills WHERE ts BETWEEN ? AND ?", (start, end), 0), Decimal(0))
    runs_ok = int(scalar(conn, "SELECT COUNT(*) FROM runs WHERE started_at BETWEEN ? AND ? AND status IN ('ok','no_candidates')", (start, end), 0))
    runs_failed = int(scalar(conn, "SELECT COUNT(*) FROM runs WHERE started_at BETWEEN ? AND ? AND status NOT IN ('ok','no_candidates','running')", (start, end), 0))
    calls = int(scalar(conn, "SELECT COUNT(*) FROM claude_calls WHERE created_at BETWEEN ? AND ? AND status != 'skipped'", (start, end), 0))
    tokens = int(scalar(conn, "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) FROM claude_calls WHERE created_at BETWEEN ? AND ?", (start, end), 0))
    b = brier_scores(conn)
    row = {
        "date": ds, "start_equity": dstr(start_eq, 4), "end_equity": dstr(end_eq, 4), "pnl": dstr((end_eq - start_eq), 4) if (start_eq is not None and end_eq is not None) else None,
        "n_orders": n_orders, "n_fills": n_fills, "n_settled": n_settled, "n_won": n_won, "fees": dstr(fees, 4), "runs_ok": runs_ok, "runs_failed": runs_failed,
        "brier_model": dstr(b["brier_model"], 4), "brier_market": dstr(b["brier_market"], 4), "n_assessed": b["n"], "claude_calls": calls, "claude_tokens": tokens, "updated_at": iso(now_utc()),
    }
    upsert(conn, "daily_stats", row, ["date"])
    return row


def render_daily_png(conn: sqlite3.Connection, out_path: Path, mode: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    snaps = all_rows(conn, "SELECT ts, equity FROM balance_snapshots ORDER BY snapshot_id")
    days = all_rows(conn, "SELECT date, pnl, n_settled, n_won FROM daily_stats ORDER BY date")
    fig, axes = plt.subplots(2, 1, figsize=(9, 7), dpi=130)
    ax = axes[0]
    if snaps:
        xs = [datetime.fromisoformat(s["ts"].replace("Z", "+00:00")) for s in snaps]
        ys = [float(s["equity"] or 0) for s in snaps]
        ax.plot(xs, ys, color="#3498db", linewidth=1.8)
        ax.fill_between(xs, ys, min(ys) * 0.98 if ys else 0, alpha=0.12, color="#3498db")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.set_title(f"[{mode.upper()}] equity", loc="left", fontsize=12, fontweight="bold")
    ax.grid(alpha=0.25)
    ax2 = axes[1]
    if days:
        labels = [d["date"][5:] for d in days]
        vals = [float(d["pnl"] or 0) for d in days]
        colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in vals]
        ax2.bar(labels, vals, color=colors)
        ax2.axhline(0, color="#7f8c8d", linewidth=0.8)
        if len(labels) > 14:
            for i, lbl in enumerate(ax2.get_xticklabels()):
                lbl.set_visible(i % max(1, len(labels) // 14) == 0)
    ax2.set_title("daily P&L", loc="left", fontsize=12, fontweight="bold")
    ax2.grid(alpha=0.25, axis="y")
    b = brier_scores(conn)
    won = int(scalar(conn, "SELECT COUNT(*) FROM settlements WHERE won = 1", (), 0))
    settled = int(scalar(conn, "SELECT COUNT(*) FROM settlements", (), 0))
    eq = snaps[-1]["equity"] if snaps else "-"
    txt = f"total ${float(eq):.2f} · settled {settled} · won {won}" if snaps else "no data yet"
    fig.text(0.01, 0.005, txt, fontsize=9, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def status_summary(conn: sqlite3.Connection, mode: str) -> dict:
    snap = one(conn, "SELECT * FROM balance_snapshots ORDER BY snapshot_id DESC LIMIT 1")
    positions = all_rows(conn, "SELECT slug, side, qty, avg_cost, mark_price, unrealized_pnl, take_profit_price, stop_loss_price FROM positions WHERE status = 'open' AND qty > 0")
    open_orders = all_rows(conn, "SELECT slug, side, qty, filled_qty, limit_price, status, expires_at FROM orders WHERE status IN ('open','partially_filled','submitted','pending_submit','unknown')")
    last_run = one(conn, "SELECT run_id, started_at, finished_at, status FROM runs ORDER BY started_at DESC LIMIT 1")
    settled = all_rows(conn, "SELECT won, realized_pnl FROM settlements")
    ctl = {r["key"]: r["value"] for r in all_rows(conn, "SELECT key, value FROM control")}
    invested = sum((D(p["avg_cost"]) or Decimal(0)) * Decimal(int(p["qty"])) for p in positions) + sum((D(o["limit_price"]) or Decimal(0)) * Decimal(int(o["qty"]) - int(o["filled_qty"] or 0)) for o in open_orders if o["side"] in ("YES", "NO"))
    eq = D(snap["equity"]) if snap else None
    return {
        "mode": mode,
        "invested": dstr(invested, 4),
        "invested_pct": dstr((invested / eq) if eq else Decimal(0), 4),
        "equity": snap["equity"] if snap else None,
        "cash": snap["cash"] if snap else None,
        "reserved": snap["reserved"] if snap else None,
        "positions": [dict(p) for p in positions],
        "open_orders": [dict(o) for o in open_orders],
        "last_run": dict(last_run) if last_run else None,
        "settled": len(settled),
        "won": sum(1 for s in settled if s["won"]),
        "realized_total": dstr(sum((D(s["realized_pnl"]) or Decimal(0)) for s in settled), 4) if settled else "0",
        "trading_enabled": ctl.get("trading_enabled"),
        "paused_until": ctl.get("paused_until"),
        "kill_reason": ctl.get("kill_reason"),
        "next_run_at": ctl.get("next_run_at"),
        "last_heartbeat": ctl.get("last_heartbeat"),
    }
