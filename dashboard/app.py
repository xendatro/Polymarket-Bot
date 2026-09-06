from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from pm.config import load_settings

settings = load_settings()
st.set_page_config(page_title="Polymarket agent", layout="wide")


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


dbs = {p.stem: p for p in sorted(settings.data_dir.glob("*.db"))}
if not dbs:
    st.warning(f"No database found in {settings.data_dir}. Run `python -m scripts.init_db` first.")
    st.stop()
mode = st.sidebar.selectbox("database", list(dbs.keys()), index=list(dbs.keys()).index(settings.mode) if settings.mode in dbs else 0)
conn = open_db(dbs[mode])
st.title(f"Polymarket agent · {mode}")

snaps = q(conn, "SELECT ts, CAST(equity AS REAL) AS total, CAST(cash AS REAL) AS cash FROM balance_snapshots ORDER BY snapshot_id")
ctl = dict(conn.execute("SELECT key, value FROM control").fetchall())
settled = q(conn, "SELECT * FROM settlements ORDER BY settlement_id DESC")
closed = q(conn, "SELECT slug, side, avg_cost, realized_pnl, closed_at FROM positions WHERE status = 'closed' ORDER BY closed_at DESC")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("total money", f"${snaps['total'].iloc[-1]:.2f}" if len(snaps) else "-")
c2.metric("cash", f"${snaps['cash'].iloc[-1]:.2f}" if len(snaps) else "-")
c3.metric("settled / won", f"{len(settled)} / {int(settled['won'].sum()) if len(settled) else 0}")
c4.metric("realized P&L", f"${settled['realized_pnl'].astype(float).sum() + closed['realized_pnl'].astype(float).sum():.2f}" if (len(settled) or len(closed)) else "$0.00")
c5.metric("trading", "on" if ctl.get("trading_enabled") == "1" else f"off ({ctl.get('kill_reason') or '-'})")

if len(snaps):
    snaps["ts"] = pd.to_datetime(snaps["ts"])
    st.line_chart(snaps.set_index("ts")[["total", "cash"]])

tabs = st.tabs(["positions & orders", "decisions", "runs", "daily", "calibration", "events"])
with tabs[0]:
    st.subheader("open positions")
    st.dataframe(q(conn, "SELECT slug, side, qty, avg_cost, mark_price, unrealized_pnl, take_profit_price, stop_loss_price, opened_at FROM positions WHERE status = 'open' AND qty > 0"), use_container_width=True)
    st.subheader("orders")
    st.dataframe(q(conn, "SELECT created_at, slug, side, intent, limit_price, qty, filled_qty, avg_fill_price, fees_paid, status, source, expires_at, error_text FROM orders ORDER BY created_at DESC LIMIT 300"), use_container_width=True)
    st.subheader("closed positions")
    st.dataframe(closed, use_container_width=True)
    st.subheader("settlements")
    st.dataframe(settled, use_container_width=True)
with tabs[1]:
    st.dataframe(q(conn, "SELECT created_at, run_id, slug, side, go, proposed_price, proposed_qty, p_market, edge_net, nogo_reasons_json, rationale FROM decisions ORDER BY decision_id DESC LIMIT 500"), use_container_width=True)
with tabs[2]:
    st.dataframe(q(conn, "SELECT started_at, finished_at, status, trigger, candidates_scanned, decisions, orders_placed, error_text FROM runs ORDER BY started_at DESC LIMIT 300"), use_container_width=True)
with tabs[3]:
    st.dataframe(q(conn, "SELECT date, start_equity, end_equity, pnl, n_orders, n_fills, n_settled, n_won, fees FROM daily_stats ORDER BY date DESC"), use_container_width=True)
with tabs[4]:
    st.caption("Realized edge of favorites the system has watched settle: mean(outcome - price) per bucket, shrunk toward the 2.5 cent prior; used for scoring and Kelly sizing once n is large enough.")
    st.dataframe(q(conn, "SELECT category, price_band, n, mean_residual, bias_est, updated_at FROM calibration ORDER BY category, price_band"), use_container_width=True)
with tabs[5]:
    st.dataframe(q(conn, "SELECT ts, actor, level, event_type, ref_table, ref_id, payload_json FROM events_log ORDER BY event_id DESC LIMIT 300"), use_container_width=True)
