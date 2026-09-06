from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests

from pm.config import Config, Settings
from pm.db import all_rows, get_control, one, set_control
from pm.notify import cents, embed, money, pct, post_webhook, signed_money
from pm.risk import account_from_db
from pm.scan import market_from_db
from pm.util import ONE, ZERO, D, iso, now_utc, parse_iso

BUY_INTENTS = ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT")
SELL_INTENTS = ("ORDER_INTENT_SELL_LONG", "ORDER_INTENT_SELL_SHORT")


def local_fmt(dt: datetime | None, tzname: str) -> str:
    if dt is None:
        return "-"
    loc = dt.astimezone(ZoneInfo(tzname))
    return f"{loc.strftime('%b')} {loc.day}, {loc.strftime('%I:%M %p').lstrip('0')} {loc.tzname()}"


def trade_name(question: str | None, title: str | None, side: str | None) -> str:
    q = (question or "").strip()
    t = (title or "").strip()
    name = q if q else t
    if q and t and t != q:
        name = f"{q}: {t}"
    return f"{name or '?'} ({side})" if side else (name or "?")


def _market_bits(conn: sqlite3.Connection, slug: str) -> tuple[str, str, datetime | None]:
    m = market_from_db(conn, slug)
    if m is None:
        return slug, "", None
    return m.question, m.title, m.event_time


def _trade_lines(name: str, et, tz: str, qty: int, price: Decimal, status_line: str) -> list[str]:
    cost = price * qty
    return [f"**{name}**", f"- **End time:** {local_fmt(et, tz)}", f"- **Bought:** {qty} contracts @ {cents(price)} ({money(cost)})", f"- **If it wins:** {signed_money((ONE - price) * qty)}", f"- **If it loses:** {signed_money(-cost)}", status_line]


def _order_status_line(o, tz: str) -> str:
    if o["status"] == "filled":
        return "- **Status:** filled"
    if o["status"] in ("open", "partially_filled", "submitted"):
        return f"- **Order expires:** {local_fmt(parse_iso(o['expires_at']), tz)} if nobody sells to us"
    return f"- **Status:** {o['status']}"


def _bought_block(conn, cfg: Config, o, tz: str) -> tuple[str, Decimal]:
    q, t, et = _market_bits(conn, o["slug"])
    qty = int(o["qty"])
    price = D(o["limit_price"]) or ZERO
    return "\n".join(_trade_lines(trade_name(q, t, o["side"]), et, tz, qty, price, _order_status_line(o, tz))), price * qty


def _sold_block(conn, o, tz: str) -> tuple[str, Decimal, Decimal]:
    q, t, _ = _market_bits(conn, o["slug"])
    qty = int(o["filled_qty"] or 0)
    price = D(o["avg_fill_price"]) or D(o["limit_price"]) or ZERO
    proceeds = price * qty
    pos = one(conn, "SELECT avg_cost FROM positions WHERE slug = ? AND side = ?", (o["slug"], o["side"]))
    entry = D(pos["avg_cost"]) if pos else None
    profit = (price - entry) * qty if entry is not None else ZERO
    lines = [f"**{trade_name(q, t, o['side'])}**", f"- **Sold:** {qty} contracts @ {cents(price)} ({money(proceeds)})", f"- **Profit:** {signed_money(profit)}" + (f" (bought @ {cents(entry)})" if entry is not None else "")]
    return "\n".join(lines), proceeds, profit


def _settled_block(conn, s) -> tuple[str, Decimal, Decimal]:
    q, t, _ = _market_bits(conn, s["slug"])
    payout = D(s["payout_total"]) or ZERO
    profit = D(s["realized_pnl"]) or ZERO
    lines = [f"**{trade_name(q, t, s['side'])}**", f"- **Settled:** {'WON' if s['won'] else 'LOST'}, {s['qty_held']} contracts paid {money(payout)}", f"- **Profit:** {signed_money(profit)}"]
    return "\n".join(lines), payout, profit


def _money_field(conn, settings: Settings, now: datetime) -> tuple[str, str, bool]:
    acct = account_from_db(conn, settings.mode, now)
    return ("Money", f"{money(acct.total)} total · {money(acct.cash)} cash · {pct(acct.invested_pct)} invested", False)


def build_trade_report(conn: sqlite3.Connection, cfg: Config, settings: Settings, since: datetime, now: datetime, seq: int) -> dict:
    tz = cfg.display_timezone
    buys = all_rows(conn, "SELECT * FROM orders WHERE created_at > ? AND created_at <= ? AND intent IN (?, ?) AND status NOT IN ('rejected') ORDER BY created_at", (iso(since), iso(now), *BUY_INTENTS))
    sells = all_rows(conn, "SELECT * FROM orders WHERE created_at > ? AND created_at <= ? AND intent IN (?, ?) AND filled_qty > 0 ORDER BY created_at", (iso(since), iso(now), *SELL_INTENTS))
    settled = all_rows(conn, "SELECT * FROM settlements WHERE detected_at > ? AND detected_at <= ? ORDER BY settlement_id", (iso(since), iso(now)))
    bought_blocks, spent = [], ZERO
    for o in buys:
        b, c = _bought_block(conn, cfg, o, tz)
        bought_blocks.append(b)
        spent += c
    sold_blocks, received, profit = [], ZERO, ZERO
    for o in sells:
        b, p, pr = _sold_block(conn, o, tz)
        sold_blocks.append(b)
        received += p
        profit += pr
    for s in settled:
        b, p, pr = _settled_block(conn, s)
        sold_blocks.append(b)
        received += p
        profit += pr
    state = get_control(conn, "exchange_state", "open")
    head = [f"**Time:** {local_fmt(now, tz)}"] + ([f"**Exchange:** trading paused ({state.replace('MARKET_STATE_', '').lower()})"] if state not in ("", "open", "MARKET_STATE_OPEN") else []) + [f"**Bought:** {len(buys)} bet{'s' if len(buys) != 1 else ''} ({money(spent)})", f"**Sold:** {len(sold_blocks)} bet{'s' if len(sold_blocks) != 1 else ''} ({money(received)}, {signed_money(profit)} profit)"]
    parts = ["\n".join(head)]
    if bought_blocks:
        parts.append("**[BOUGHT]**\n\n" + "\n\n".join(bought_blocks))
    if sold_blocks:
        parts.append("**[SOLD]**\n\n" + "\n\n".join(sold_blocks))
    if not bought_blocks and not sold_blocks:
        parts.append("No trades this hour.")
    color = "green" if (bought_blocks or sold_blocks) else "grey"
    return embed(f"Trade Report #{seq}", "\n\n".join(parts), color, [_money_field(conn, settings, now)], footer=f"{settings.mode} · hourly cycle")


def build_positions_card(conn: sqlite3.Connection, cfg: Config, settings: Settings, now: datetime) -> dict:
    tz = cfg.display_timezone
    blocks = []
    for p in all_rows(conn, "SELECT * FROM positions WHERE status = 'open' AND qty > 0 ORDER BY opened_at"):
        q, t, et = _market_bits(conn, p["slug"])
        entry = D(p["avg_cost"]) or ZERO
        status_line = "- **Status:** filled" + (f", sells early at {cents(p['take_profit_price'])}" if p["take_profit_price"] else "") + (f", stop at {cents(p['stop_loss_price'])}" if p["stop_loss_price"] else "")
        if p["mark_price"]:
            status_line += f" · now {cents(p['mark_price'])} ({signed_money((D(p['mark_price']) - entry) * int(p['qty']))})"
        blocks.append("\n".join(_trade_lines(trade_name(q, t, p["side"]), et, tz, int(p["qty"]), entry, status_line)))
    for o in all_rows(conn, "SELECT * FROM orders WHERE status IN ('open', 'partially_filled', 'submitted') AND intent IN (?, ?) ORDER BY created_at", BUY_INTENTS):
        q, t, et = _market_bits(conn, o["slug"])
        qty = int(o["qty"]) - int(o["filled_qty"] or 0)
        price = D(o["limit_price"]) or ZERO
        blocks.append("\n".join(_trade_lines(trade_name(q, t, o["side"]), et, tz, qty, price, _order_status_line(o, tz))))
    body = "\n\n".join(blocks) if blocks else "No open trades."
    return embed(f"Current Trades ({len(blocks)})", body, "blue", [_money_field(conn, settings, now)], footer=f"{settings.mode} · updated {local_fmt(now, tz)}")


def upsert_webhook_message(url: str | None, e: dict, message_id: str | None) -> str | None:
    if not url:
        return None
    try:
        if message_id:
            r = requests.patch(f"{url}/messages/{message_id}", json={"embeds": [e]}, timeout=30)
            if 200 <= r.status_code < 300:
                return message_id
        r = requests.post(url, params={"wait": "true"}, json={"embeds": [e]}, timeout=30)
        if 200 <= r.status_code < 300:
            return str(r.json().get("id") or "") or None
    except Exception:
        return None
    return None


def post_hourly_reports(conn: sqlite3.Connection, cfg: Config, settings: Settings, now: datetime | None = None) -> None:
    now = now or now_utc()
    since = parse_iso(get_control(conn, "last_report_at", "") or None) or (now - timedelta(hours=float(cfg.run_interval_hours)))
    seq = int(get_control(conn, "report_seq", "0") or 0) + 1
    post_webhook(settings.webhook_trades, embeds=[build_trade_report(conn, cfg, settings, since, now, seq)], conn=conn, channel="trades", dedupe_key=f"report:{seq}")
    set_control(conn, "report_seq", str(seq))
    set_control(conn, "last_report_at", iso(now))
    mid = upsert_webhook_message(settings.webhook_runs, build_positions_card(conn, cfg, settings, now), get_control(conn, "positions_message_id", "") or None)
    if mid:
        set_control(conn, "positions_message_id", mid)
