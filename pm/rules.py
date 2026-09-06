from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from pm.client import PMClient, market_quotes
from pm.config import Config
from pm.calibrate import bias_for
from pm.db import insert
from pm.models import INTENT_BY_SIDE, Account, ControlState, Decision, MarketInfo, Quote
from pm.risk import ABS_MAX_QTY, ABS_MAX_TRADE_PCT_EQUITY, fee_per_contract, proposed_price
from pm.scan import upsert_market
from pm.util import ONE, ZERO, D, dstr, dumps, iso, now_utc


@dataclass
class Pick:
    market: MarketInfo
    side: str
    price: Decimal
    spread: Decimal
    days: Decimal
    open_interest: Decimal | None = None
    score: Decimal = ZERO
    quote: Quote | None = None
    features: dict = field(default_factory=dict)
    candidate_id: int | None = None
    bias: Decimal | None = None


def score_pick(cfg: Config, price: Decimal, spread: Decimal, days: Decimal, open_interest: Decimal | None, bias: Decimal | None = None) -> Decimal:
    f = cfg.favorites
    b = f.bias if bias is None else bias
    liquidity = min(ONE, (open_interest or ZERO) / f.oi_full) if f.oi_full > 0 else ONE
    daily_return = (b / price) / max(days, Decimal("0.5"))
    exit_cost = (spread / price) / 2
    return liquidity * (daily_return - exit_cost)


def eligible_side(cfg: Config, bid: Decimal, ask: Decimal) -> tuple[str, Decimal] | None:
    f = cfg.favorites
    if f.price_min <= ask <= f.price_max:
        return "YES", ask
    no_ask = ONE - bid
    if f.price_min <= no_ask <= f.price_max:
        return "NO", no_ask
    return None


def scan_favorites(conn: sqlite3.Connection, client: PMClient, cfg: Config, run_id: str, now: datetime | None = None) -> tuple[int, list[Pick]]:
    now = now or now_utc()
    f = cfg.favorites
    window_end = now + timedelta(days=float(max(f.max_days, f.fetch_window_days)))
    seen: dict[str, dict] = {}
    for cat in cfg.scan.categories:
        if cat in cfg.no_trade_categories:
            continue
        for m in client.list_markets(max_items=cfg.scan.max_markets, active=True, closed=False, categories=[cat], endDateMin=iso(now), endDateMax=iso(window_end)):
            if m.get("slug"):
                seen[m["slug"]] = m
    picks: list[Pick] = []
    for slug, m in seen.items():
        info = upsert_market(conn, m, now)
        if info.status not in ("", "MARKET_STATUS_OPEN") or info.closed or m.get("hidden"):
            continue
        if info.category in cfg.no_trade_categories or (info.market_type and info.market_type in cfg.no_trade_market_types):
            continue
        bid, ask = market_quotes(m)
        if bid is None or ask is None or bid <= ZERO or ask >= ONE:
            continue
        spread = ask - bid
        if spread > f.max_spread:
            continue
        event_time = info.event_time
        if event_time is None:
            continue
        hours = Decimal((event_time - now).total_seconds()) / 3600
        if hours < f.min_hours or hours > f.max_days * 24:
            continue
        if info.frozen_at(now, cfg.exits.freeze_before_game_minutes):
            continue
        es = eligible_side(cfg, bid, ask)
        if es is None:
            continue
        side, price = es
        days = hours / 24
        feats = {"yes_bid": dstr(bid), "yes_ask": dstr(ask), "spread": dstr(spread), "days_to_settle": dstr(days, 2), "category": info.category, "market_type": info.market_type, "event_time": iso(event_time)}
        picks.append(Pick(info, side, price, spread, days, None, score_pick(cfg, price, spread, days, None), None, feats))
    picks.sort(key=lambda p: p.score, reverse=True)
    checked: list[Pick] = []
    for p in picks[: f.check_top_n]:
        bbo = client.get_bbo(p.market.slug)
        oi = D((bbo or {}).get("openInterest"))
        p.open_interest = oi
        p.features["open_interest"] = dstr(oi)
        if oi is None or oi < f.min_open_interest:
            continue
        b, n, src = bias_for(conn, cfg, p.market.category, p.price)
        p.bias = b
        p.features["bias"] = dstr(b, 4)
        p.features["bias_source"] = src
        if b <= ZERO:
            continue
        p.score = score_pick(cfg, p.price, p.spread, p.days, oi, b)
        checked.append(p)
    checked.sort(key=lambda p: p.score, reverse=True)
    for i, p in enumerate(checked, start=1):
        p.features["score"] = dstr(p.score, 4)
        p.candidate_id = insert(conn, "candidates", {"run_id": run_id, "slug": p.market.slug, "snapshot_id": None, "strategy": "favorites", "side": p.side, "scan_score": dstr(p.score, 4), "rank": i, "researched": 0, "features_json": dumps(p.features), "created_at": iso(now)})
    return len(seen), checked


def decide_favorite(cfg: Config, p: Pick, quote: Quote, account: Account, control: ControlState, now: datetime, category_counts: dict[str, int], event_counts: dict[str, int]) -> Decision:
    reasons: list[str] = []
    m = p.market
    d = Decision(slug=m.slug, source="rules", go=False, nogo_reasons=reasons, side=p.side, intent=INTENT_BY_SIDE[p.side], snapshot_id=quote.snapshot_id, tick=m.tick, fee_coef=m.fee_coef, tier="favorite")
    d.equity_at_decision, d.exposure_at_decision = account.total, account.invested
    bid, ask = quote.side_prices(p.side)
    if bid is None or ask is None or bid <= ZERO:
        reasons.append("no_two_sided_quote")
        return d
    d.best_bid, d.best_ask, d.spread = bid, ask, ask - bid
    d.p_market = (bid + ask) / 2
    bias = p.bias if p.bias is not None else cfg.favorites.bias
    d.p_model_raw = d.p_market + bias
    d.p_model_shrunk = d.p_model_raw
    price, order_type, post_only = proposed_price(cfg, bid, ask, m.tick)
    d.proposed_price, d.order_type, d.post_only = price, order_type, post_only
    d.tif = cfg.order_defaults.tif if order_type == "limit" else "IOC"
    d.fee_per_contract = fee_per_contract(price, m.fee_coef)
    d.edge_net = d.p_model_shrunk - price - d.fee_per_contract
    d.kelly_full = max(ZERO, (d.p_model_shrunk - price) / (ONE - price)) if price < ONE else ZERO
    d.kelly_used = d.kelly_full
    f = cfg.favorites
    if not (f.price_min <= ask <= f.price_max):
        reasons.append("price_out_of_range")
    if d.spread > f.max_spread:
        reasons.append("spread_too_wide")
    if quote.open_interest is not None and quote.open_interest < f.min_open_interest:
        reasons.append("open_interest_low")
    if m.frozen_at(now, cfg.exits.freeze_before_game_minutes):
        reasons.append("game_imminent")
    if m.slug in account.held_slugs:
        reasons.append("already_holding")
    if m.slug in account.open_order_slugs:
        reasons.append("open_order_exists")
    if not control.trading_enabled:
        reasons.append("trading_disabled")
    if control.paused_until is not None and control.paused_until > now:
        reasons.append(f"paused_{control.pause_reason or 'unknown'}")
    if control.in_maintenance:
        reasons.append("maintenance_window")
    if account.open_positions >= cfg.max_open_positions:
        reasons.append("max_open_positions")
    if account.orders_today >= cfg.max_new_orders_per_day:
        reasons.append("max_orders_per_day")
    cat = m.category or "other"
    cat_cap = max(1, int((Decimal(cfg.max_open_positions) * f.max_positions_per_category_pct).to_integral_value(rounding=ROUND_DOWN)))
    if category_counts.get(cat, 0) >= cat_cap:
        reasons.append("category_share_cap")
    ev = event_key(m)
    if event_counts.get(ev, 0) >= f.max_per_event:
        reasons.append("event_cap")
    total = account.total
    budget_left = cfg.portfolio.invest_target_pct * total - account.invested
    if budget_left <= ZERO:
        reasons.append("portfolio_target_reached")
    qty = 0
    if price > ZERO and budget_left > ZERO:
        per_position_cap = min(cfg.portfolio.max_position_pct_of_total, ABS_MAX_TRADE_PCT_EQUITY) * total
        if total >= cfg.sizing.kelly_switch_total and price < ONE:
            kelly_dollars = cfg.sizing.kelly_multiplier * (bias / (ONE - price)) * total
            per_position_cap = min(per_position_cap, max(kelly_dollars, price))
            d.kelly_used = cfg.sizing.kelly_multiplier * (bias / (ONE - price))
        caps = [Decimal(ABS_MAX_QTY), per_position_cap / price, budget_left / price, (account.cash - cfg.portfolio.min_cash_usd) / price]
        qty = int(max(ZERO, min(caps)).to_integral_value(rounding=ROUND_DOWN))
        if qty < 1:
            reasons.append("size_below_minimum")
            qty = 0
    d.proposed_qty = qty
    if qty > 0 and quote.displayed_depth() < cfg.liquidity.displayed_size_multiple * qty:
        reasons.append("insufficient_displayed_depth")
    exp = now + timedelta(hours=float(cfg.order_defaults.gtd_hours))
    if m.end_date is not None:
        exp = min(exp, m.end_date - timedelta(minutes=30))
    if m.is_game:
        exp = min(exp, m.game_start_time - timedelta(minutes=5))
    d.expires_at = exp
    d.rationale = f"favorite at {dstr(price)}, bias {dstr(bias, 4)}, settles in {dstr(p.days, 1)}d, OI {dstr(p.open_interest, 0)}, score {dstr(p.score, 4)}"
    d.go = not reasons and qty > 0
    return d


def event_key(m: MarketInfo) -> str:
    if m.is_game:
        return f"{m.category}:{iso(m.game_start_time)}"
    return (m.question or m.slug).strip().lower()
