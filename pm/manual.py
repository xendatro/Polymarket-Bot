from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from pm.client import PMClient
from pm.config import Config, Settings
from pm.db import insert, one
from pm.models import EXIT_INTENT_BY_SIDE, INTENT_BY_SIDE, Decision, MarketInfo, Quote
from pm.risk import ABS_MAX_ORDERS_PER_DAY, ABS_MAX_PRICE, ABS_MAX_QTY, ABS_MAX_TRADE_PCT_EQUITY, account_from_db, control_state, fee_per_contract
from pm.scan import fetch_quote, record_snapshot, upsert_market
from pm.util import ONE, ZERO, D, iso, round_to_tick


def build_manual_decision(settings: Settings, cfg: Config, cfg_hash: str, conn: sqlite3.Connection, client: PMClient, proposal: dict, now: datetime, source: str = "discord") -> tuple[Decision, MarketInfo | None, Quote | None, list[str]]:
    reasons: list[str] = []
    slug = str(proposal.get("slug") or "").strip()
    side = str(proposal.get("side") or "YES").upper()
    action = str(proposal.get("action") or "buy").lower()
    qty = int(proposal.get("qty") or 0)
    price = D(proposal.get("price"))
    d = Decision(slug=slug, source=source, go=False, nogo_reasons=reasons, side=side, proposed_qty=qty, rationale=str(proposal.get("rationale") or "")[:700])
    raw = client.get_market(slug) if slug else None
    if raw is None:
        reasons.append("market_not_found")
        d.decision_id = insert(conn, "decisions", d.to_row(None, cfg_hash, iso(now)))
        return d, None, None, reasons
    market = upsert_market(conn, raw, now)
    quote = fetch_quote(client, slug)
    if quote is None:
        reasons.append("no_quote")
        d.decision_id = insert(conn, "decisions", d.to_row(None, cfg_hash, iso(now)))
        return d, market, None, reasons
    record_snapshot(conn, slug, quote, "manual", now)
    d.snapshot_id = quote.snapshot_id
    d.tick, d.fee_coef = market.tick, market.fee_coef
    bid, ask = quote.side_prices(side)
    d.best_bid, d.best_ask = bid, ask
    if bid is not None and ask is not None:
        d.spread = ask - bid
        d.p_market = (bid + ask) / 2
    if price is None or price <= ZERO or price >= ONE:
        reasons.append("invalid_price")
        price = ask if action == "buy" else bid
    if price is None:
        reasons.append("no_price")
        d.decision_id = insert(conn, "decisions", d.to_row(None, cfg_hash, iso(now)))
        return d, market, quote, reasons
    price = round_to_tick(price, market.tick)
    d.proposed_price = price
    d.fee_per_contract = fee_per_contract(price, market.fee_coef)
    d.order_type = "limit"
    d.post_only = False
    d.tif = "GTD" if action == "buy" else "IOC"
    d.expires_at = (now + timedelta(hours=float(cfg.order_defaults.gtd_hours))) if action == "buy" else None
    account = account_from_db(conn, settings.mode, now)
    cs = control_state(conn, cfg, now)
    d.equity_at_decision, d.exposure_at_decision = account.equity, account.exposure
    if qty <= 0:
        reasons.append("qty_must_be_positive")
    if qty > ABS_MAX_QTY:
        reasons.append(f"qty_above_absolute_max_{ABS_MAX_QTY}")
    if market.resolved or market.status not in ("", "MARKET_STATUS_OPEN"):
        reasons.append(f"market_status_{market.status or 'unknown'}")
    if not cs.trading_enabled:
        reasons.append(f"trading_disabled:{cs.kill_reason}")
    if cs.paused_until is not None and cs.paused_until > now:
        reasons.append(f"paused_{cs.pause_reason}")
    if cs.in_maintenance:
        reasons.append("maintenance_window")
    if action == "buy":
        d.intent = INTENT_BY_SIDE[side]
        if price > min(cfg.never_buy_above, ABS_MAX_PRICE):
            reasons.append("price_above_cap")
        cost = price * qty
        if cost > min(cfg.portfolio.max_position_pct_of_total, ABS_MAX_TRADE_PCT_EQUITY) * account.equity:
            reasons.append("cost_above_max_trade")
        if account.invested + cost > cfg.portfolio.invest_target_pct * account.equity:
            reasons.append("portfolio_target_reached")
        if account.cash - cost < cfg.portfolio.min_cash_usd:
            reasons.append("below_bankroll_floor")
        if account.orders_today >= min(cfg.max_new_orders_per_day, ABS_MAX_ORDERS_PER_DAY):
            reasons.append("max_orders_per_day")
        if slug in account.held_slugs:
            reasons.append("already_holding")
        if market.end_date is not None and market.end_date - now < timedelta(hours=float(cfg.favorites.min_hours)):
            reasons.append("closes_too_soon")
    else:
        d.intent = EXIT_INTENT_BY_SIDE[side]
        pos = one(conn, "SELECT qty FROM positions WHERE slug = ? AND side = ? AND status = 'open'", (slug, side))
        if pos is None or int(pos["qty"]) < qty:
            reasons.append("insufficient_position")
    d.tier = "manual"
    d.go = not reasons
    d.decision_id = insert(conn, "decisions", d.to_row(None, cfg_hash, iso(now)))
    return d, market, quote, reasons
