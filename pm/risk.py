from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from pm.config import Config
from pm.models import Account, ControlState
from pm.util import ONE, ZERO, D, clamp, round_to_tick

ABS_MAX_TRADE_PCT_EQUITY = Decimal("0.5")
ABS_MAX_OPEN_POSITIONS = 12
ABS_MAX_PRICE = Decimal("0.97")
ABS_MAX_QTY = 50
ABS_MAX_ORDERS_PER_DAY = 20
ABS_MIN_EQUITY_TO_TRADE = Decimal("1.00")
MAKER_REBATE_COEF = Decimal("0.0125")
ET = ZoneInfo("America/New_York")


def fee_per_contract(price: Decimal, coef: Decimal = Decimal("0.06")) -> Decimal:
    return coef * price * (ONE - price)


def taker_fee(price: Decimal, qty: int | Decimal, coef: Decimal = Decimal("0.06")) -> Decimal:
    return fee_per_contract(price, coef) * Decimal(qty)


def maker_rebate(price: Decimal, qty: int | Decimal, coef: Decimal = MAKER_REBATE_COEF) -> Decimal:
    return coef * price * (ONE - price) * Decimal(qty)


def breakeven_probability(price: Decimal, coef: Decimal = Decimal("0.06")) -> Decimal:
    return price + fee_per_contract(price, coef)


def kelly_fraction(p: Decimal, price: Decimal) -> Decimal:
    if price >= ONE or price <= ZERO:
        return ZERO
    return max(ZERO, (p - price) / (ONE - price))


def shrink(p_model: Decimal, p_market: Decimal, lam: Decimal) -> Decimal:
    return clamp(p_market + lam * (p_model - p_market), ZERO, ONE)


def in_maintenance_window(cfg: Config, now: datetime) -> bool:
    mw = cfg.maintenance_window_et
    local = now.astimezone(ET)
    if local.weekday() != mw.weekday:
        return False
    sh, sm = (int(x) for x in mw.start.split(":"))
    eh, em = (int(x) for x in mw.end.split(":"))
    t = local.time()
    return time(sh, sm) <= t < time(eh, em)


def proposed_price(cfg: Config, bid: Decimal, ask: Decimal, tick: Decimal) -> tuple[Decimal, str, bool]:
    rule = cfg.order_defaults.price_rule
    if cfg.order_defaults.type == "market" or rule == "best_ask":
        return round_to_tick(ask, tick), "market" if cfg.order_defaults.type == "market" else "limit", False
    if rule == "mid":
        px = round_to_tick((bid + ask) / 2, tick, ROUND_DOWN)
        return min(px, ask - tick) if px >= ask else px, "limit", cfg.order_defaults.post_only
    px = bid + tick
    if px >= ask:
        px = bid
    return round_to_tick(px, tick, ROUND_DOWN), "limit", cfg.order_defaults.post_only


def account_from_db(conn, mode: str, now: datetime) -> Account:
    from pm.db import all_rows, scalar

    if mode == "paper":
        cash = D(scalar(conn, "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM paper_ledger"), ZERO)
        cash = Decimal(str(round(float(cash), 4)))
    else:
        row = conn.execute("SELECT cash, buying_power FROM balance_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        cash = D(row["cash"], ZERO) if row else ZERO
    pos = all_rows(conn, "SELECT slug, side, qty, cost_basis, mark_price FROM positions WHERE status = 'open' AND qty > 0")
    exposure = ZERO
    mark_total = ZERO
    held = set()
    for r in pos:
        held.add(r["slug"])
        cb = D(r["cost_basis"], ZERO)
        exposure += cb
        mk = D(r["mark_price"])
        mark_total += (mk * Decimal(r["qty"])) if mk is not None else cb
    open_orders = all_rows(conn, "SELECT slug, limit_price, qty, filled_qty, intent FROM orders WHERE status IN ('pending_submit','submitted','open','partially_filled','cancel_requested','unknown')")
    reserved = ZERO
    open_slugs = set()
    for r in open_orders:
        open_slugs.add(r["slug"])
        if r["intent"] in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
            reserved += D(r["limit_price"], ZERO) * Decimal(int(r["qty"]) - int(r["filled_qty"] or 0))
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    orders_today = int(scalar(conn, "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND status NOT IN ('rejected') AND intent IN ('ORDER_INTENT_BUY_LONG','ORDER_INTENT_BUY_SHORT')", (day_start,), 0))
    equity = cash + mark_total
    return Account(cash=cash, equity=equity, exposure=exposure, reserved=reserved, open_positions=len(pos), orders_today=orders_today, held_slugs=held, open_order_slugs=open_slugs, positions_mark=mark_total)


def control_state(conn, cfg: Config, now: datetime) -> ControlState:
    from pm.db import get_control, scalar
    from pm.util import parse_iso

    enabled = get_control(conn, "trading_enabled", "0") == "1"
    paused_until = parse_iso(get_control(conn, "paused_until", "") or None)
    pause_reason = get_control(conn, "pause_reason", "")
    kill_reason = get_control(conn, "kill_reason", "")
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    daily = D(scalar(conn, "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) FROM settlements WHERE detected_at >= ?", (day_start,)), ZERO)
    daily += D(scalar(conn, "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) FROM positions WHERE status = 'closed' AND closed_at >= ?", (day_start,)), ZERO)
    equity = D(scalar(conn, "SELECT equity FROM balance_snapshots ORDER BY snapshot_id DESC LIMIT 1"), ZERO) or ZERO
    window_start = (now - timedelta(days=cfg.halt_after_losses.window_days)).isoformat()
    losses = int(scalar(conn, "SELECT COUNT(*) FROM settlements WHERE won = 0 AND detected_at >= ?", (window_start,), 0))
    last_loss = parse_iso(scalar(conn, "SELECT MAX(detected_at) FROM settlements WHERE won = 0"))
    computed_pause: datetime | None = None
    computed_reason = ""
    if equity > ZERO and daily <= -(cfg.daily_loss_stop_pct * equity):
        computed_pause = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        computed_reason = "daily_loss_stop"
    if cfg.loss_cooldown_hours > 0 and last_loss is not None and now - last_loss < timedelta(hours=float(cfg.loss_cooldown_hours)):
        cand = last_loss + timedelta(hours=float(cfg.loss_cooldown_hours))
        if computed_pause is None or cand > computed_pause:
            computed_pause, computed_reason = cand, "loss_cooldown"
    if computed_pause is not None and (paused_until is None or computed_pause > paused_until):
        paused_until, pause_reason = computed_pause, computed_reason
    if losses >= cfg.halt_after_losses.count and enabled:
        enabled = False
        kill_reason = kill_reason or f"{losses}_losses_in_{cfg.halt_after_losses.window_days}d"
    return ControlState(
        trading_enabled=enabled,
        paused_until=paused_until,
        pause_reason=pause_reason,
        kill_reason=kill_reason,
        daily_realized_pnl=daily,
        recent_losses=losses,
        last_loss_at=last_loss,
        in_maintenance=in_maintenance_window(cfg, now),
    )
