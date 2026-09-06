from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from pm.client import PMClient
from pm.config import Config
from pm.db import all_rows, insert, log_event, one, update
from pm.models import MarketInfo
from pm.scan import upsert_market
from pm.util import ONE, ZERO, D, dstr, iso, now_utc, parse_iso


def _resolve_market(client: PMClient, conn: sqlite3.Connection, slug: str, now: datetime) -> tuple[MarketInfo | None, Decimal | None]:
    m = client.get_market(slug)
    if m is None:
        return None, None
    info = upsert_market(conn, m, now)
    if not info.resolved:
        return info, None
    price = client.get_settlement(slug)
    if price is None:
        for side in m.get("marketSides") or []:
            if side.get("long") is True and side.get("price") is not None:
                price = D(side.get("price"))
    if price is not None:
        outcome = "YES" if price >= Decimal("0.5") else "NO"
        update(conn, "markets", {"slug": slug}, {"resolved_outcome": outcome, "settlement_price": dstr(price), "settled_at": iso(now)})
    return info, price


def settle_positions(conn: sqlite3.Connection, client: PMClient, cfg: Config, mode: str, now: datetime | None = None) -> list[dict]:
    now = now or now_utc()
    events: list[dict] = []
    for r in all_rows(conn, "SELECT * FROM positions WHERE status = 'open' AND qty > 0"):
        info, price = _resolve_market(client, conn, r["slug"], now)
        if info is None or price is None:
            continue
        side = r["side"]
        qty = int(r["qty"])
        payout_per = price if side == "YES" else ONE - price
        payout = payout_per * qty
        cost_basis = D(r["cost_basis"], ZERO) or ZERO
        fees = D(one(conn, "SELECT COALESCE(SUM(CAST(fee AS REAL)), 0) AS f FROM fills WHERE slug = ?", (r["slug"],))["f"], ZERO) or ZERO
        realized = payout - cost_basis
        won = 1 if realized > 0 else 0
        entry = one(conn, "SELECT d.p_model_shrunk, d.p_market, d.created_at FROM decisions d WHERE d.decision_id = ?", (r["entry_decision_id"],)) if r["entry_decision_id"] else None
        opened = parse_iso(r["opened_at"]) or now
        insert(conn, "settlements", {
            "slug": r["slug"], "side": side, "resolved_outcome": "YES" if price >= Decimal("0.5") else "NO", "settlement_price": dstr(price), "resolved_at": iso(now), "detected_at": iso(now),
            "qty_held": qty, "payout_total": dstr(payout, 6), "cost_basis": dstr(cost_basis, 6), "fees_total": dstr(fees, 6), "realized_pnl": dstr(realized, 6), "won": won,
            "entry_decision_id": r["entry_decision_id"], "p_model_at_entry": entry["p_model_shrunk"] if entry else None, "p_market_at_entry": entry["p_market"] if entry else None, "days_held": dstr(Decimal((now - opened).total_seconds()) / 86400, 2),
        })
        update(conn, "positions", {"slug": r["slug"], "side": side}, {"qty": 0, "status": "settled", "closed_at": iso(now), "mark_price": dstr(payout_per), "realized_pnl": dstr(realized, 6), "updated_at": iso(now)})
        if mode == "paper" and payout > 0:
            insert(conn, "paper_ledger", {"ts": iso(now), "type": "settlement", "amount": dstr(payout, 6), "ref_table": "settlements", "ref_id": r["slug"], "note": f"{side} settled at {dstr(price)}"})
        for o in all_rows(conn, "SELECT order_id FROM orders WHERE slug = ? AND status IN ('open','partially_filled','submitted')", (r["slug"],)):
            update(conn, "orders", {"order_id": o["order_id"]}, {"status": "expired", "cancel_reason": "market_resolved", "closed_at": iso(now), "updated_at": iso(now)})
        log_event(conn, "settle", "info", "position_settled", "settlements", r["slug"], {"side": side, "won": won, "realized": dstr(realized, 4)})
        events.append({"slug": r["slug"], "side": side, "qty": qty, "won": bool(won), "realized_pnl": realized, "settlement_price": price, "question": info.question, "title": info.title})
    return events


