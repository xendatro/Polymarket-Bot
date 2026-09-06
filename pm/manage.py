from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from pm.client import PMClient
from pm.config import Config, Settings
from pm.db import all_rows, log_event, one, update
from pm.execution import exit_position
from pm.scan import fetch_quote, market_from_db, record_snapshot
from pm.util import ZERO, D, dstr, iso, now_utc


def exit_levels(cfg: Config, entry: Decimal) -> tuple[Decimal, Decimal]:
    take_profit = min(entry + cfg.exits.take_profit_cents, cfg.exits.take_profit_price)
    stop_loss = entry - cfg.exits.stop_loss_cents
    return take_profit, stop_loss


def manage_positions(settings: Settings, cfg: Config, conn: sqlite3.Connection, client: PMClient, broker, now: datetime | None = None) -> list[dict]:
    now = now or now_utc()
    events: list[dict] = []
    for p in all_rows(conn, "SELECT * FROM positions WHERE status = 'open' AND qty > 0"):
        slug, side = p["slug"], p["side"]
        market = market_from_db(conn, slug)
        if market is None:
            continue
        entry = D(p["avg_cost"])
        if entry is None:
            continue
        take_profit, stop_loss = exit_levels(cfg, entry)
        update(conn, "positions", {"slug": slug, "side": side}, {"take_profit_price": dstr(take_profit), "stop_loss_price": dstr(stop_loss)})
        if market.frozen_at(now, cfg.exits.freeze_before_game_minutes):
            events.append({"type": "frozen", "slug": slug, "side": side, "title": market.title, "question": market.question})
            continue
        if one(conn, "SELECT order_id FROM orders WHERE slug = ? AND intent IN ('ORDER_INTENT_SELL_LONG','ORDER_INTENT_SELL_SHORT') AND status IN ('pending_submit','submitted','open','partially_filled','unknown')", (slug,)):
            continue
        q = fetch_quote(client, slug, with_bbo=False)
        if q is None:
            continue
        record_snapshot(conn, slug, q, "manage", now)
        bid, ask = q.side_prices(side)
        if bid is None or bid <= ZERO:
            continue
        update(conn, "positions", {"slug": slug, "side": side}, {"mark_price": dstr(bid), "unrealized_pnl": dstr((bid - entry) * Decimal(int(p["qty"])), 6), "updated_at": iso(now)})
        reason = None
        if bid >= take_profit:
            reason = "take_profit"
        elif bid <= stop_loss:
            reason = "stop_loss"
        if reason is None:
            continue
        text = f"{'Take profit' if reason == 'take_profit' else 'Stop loss'}: bought at {dstr(entry)}, bid now {dstr(bid)} (target {dstr(take_profit)}, stop {dstr(stop_loss)})"
        pr = exit_position(settings, cfg, conn, broker, slug, side, market, q, None, f"manage_{reason}", text)
        log_event(conn, "manage", "info", reason, "positions", slug, {"side": side, "entry": dstr(entry), "bid": dstr(bid), "status": pr.status, "reason": pr.reason})
        events.append({"type": reason, "slug": slug, "side": side, "qty": int(p["qty"]), "price": bid, "entry": entry, "status": pr.status, "reason": pr.reason, "title": market.title, "question": market.question, "pnl": (bid - entry) * Decimal(int(p["qty"]))})
    return events
