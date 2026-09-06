from __future__ import annotations

import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import requests

from pm.db import insert, one, update
from pm.util import D, dstr, dumps, iso, now_utc

COLORS = {"green": 0x2ECC71, "red": 0xE74C3C, "blue": 0x3498DB, "grey": 0x95A5A6, "orange": 0xE67E22, "purple": 0x9B59B6}

STATUS_WORDS = {
    "open": "resting on the order book",
    "submitted": "submitted, waiting for the exchange",
    "partially_filled": "partly filled",
    "filled": "filled",
    "rejected": "rejected",
    "expired": "expired without filling",
    "cancelled": "cancelled",
    "cancel_requested": "cancel requested",
    "unknown": "status unknown, will be checked next cycle",
    "dry_run": "not placed (dry run)",
    "pending_submit": "about to be submitted",
}

TIER_WORDS = {"A_near_certain": "near-certain", "B_mispricing": "mispricing", "short_term": "short-term", "favorite": "favorite", "manual": "manual"}


def post_webhook(url: str | None, content: str | None = None, embeds: list[dict] | None = None, file_path: Path | str | None = None, conn: sqlite3.Connection | None = None, channel: str = "misc", dedupe_key: str | None = None) -> bool:
    if not url:
        return False
    if conn is not None and dedupe_key:
        existing = one(conn, "SELECT notif_id, posted_at FROM notifications WHERE dedupe_key = ?", (dedupe_key,))
        if existing is not None and existing["posted_at"]:
            return True
    payload: dict = {}
    if content:
        payload["content"] = content[:1900]
    if embeds:
        payload["embeds"] = embeds[:10]
    notif_id = None
    if conn is not None:
        if dedupe_key:
            existing = one(conn, "SELECT notif_id FROM notifications WHERE dedupe_key = ?", (dedupe_key,))
            notif_id = existing["notif_id"] if existing else None
        if notif_id is None:
            notif_id = insert(conn, "notifications", {"channel": channel, "dedupe_key": dedupe_key, "payload_json": dumps(payload), "attachment_path": str(file_path) if file_path else None, "created_at": iso(now_utc())})
    try:
        if file_path:
            with open(file_path, "rb") as fh:
                r = requests.post(url, data={"payload_json": dumps(payload)}, files={"file": (Path(file_path).name, fh)}, timeout=30)
        else:
            r = requests.post(url, json=payload, timeout=30)
        ok = 200 <= r.status_code < 300
        err = None if ok else f"{r.status_code}: {r.text[:200]}"
    except Exception as e:
        ok, err = False, str(e)[:200]
    if conn is not None and notif_id is not None:
        update(conn, "notifications", {"notif_id": notif_id}, {"posted_at": iso(now_utc()) if ok else None, "attempts": 1, "error_text": err})
    return ok


def embed(title: str, description: str = "", color: str = "blue", fields: list[tuple[str, str, bool]] | None = None, footer: str | None = None) -> dict:
    e: dict = {"title": title[:256], "description": description[:4000], "color": COLORS.get(color, COLORS["blue"]), "timestamp": iso(now_utc())}
    if fields:
        e["fields"] = [{"name": n[:256], "value": (v or "-")[:1024], "inline": inline} for n, v, inline in fields[:25]]
    if footer:
        e["footer"] = {"text": footer[:2048]}
    return e


def money(x) -> str:
    v = D(x)
    if v is None:
        return "-"
    return f"${float(v):,.2f}"


def signed_money(x) -> str:
    v = D(x)
    if v is None:
        return "-"
    return f"{'+' if v >= 0 else '-'}${abs(float(v)):,.2f}"


def cents(x) -> str:
    v = D(x)
    if v is None:
        return "-"
    c = v * 100
    return f"{int(c)}¢" if c == c.to_integral_value() else f"{float(c):.1f}¢"


def pct(x) -> str:
    v = D(x)
    if v is None:
        return "-"
    return f"{float(v) * 100:.0f}%"


def side_word(side: str | None) -> str:
    return "YES" if side == "YES" else "NO" if side == "NO" else "?"


def status_word(status: str | None) -> str:
    return STATUS_WORDS.get(status or "", status or "-")


def tier_word(tier: str | None) -> str:
    return TIER_WORDS.get(tier or "", tier or "no trade type")


def reason_word(code: str) -> str:
    tier = None
    body = code
    if ":" in code:
        t, body = code.split(":", 1)
        tier = TIER_WORDS.get(t, t)
    m = re.match(r"([a-z_]+?)_(?:below|above|over)_([0-9.]+)(d?)$", body)
    if body.startswith("recommendation_"):
        rec = body.split("_", 1)[1]
        text = {"skip": "Claude recommended not buying", "hold": "Claude recommended holding, not buying more", "exit": "Claude recommended exiting"}.get(rec, f"Claude recommended {rec}")
    elif m:
        what, num, days = m.groups()
        val = Decimal(num)
        if what == "p_shrunk":
            text = f"blended probability under {pct(val)}"
        elif what == "edge_net":
            text = f"expected profit under {cents(val)} per contract"
        elif what == "spread":
            text = f"bid/ask gap wider than {cents(val)}"
        elif what == "expected_resolution":
            text = f"settlement expected more than {num} days away"
        else:
            text = body.replace("_", " ")
    elif body.startswith("confidence_below_"):
        text = f"Claude's confidence below '{body.rsplit('_', 1)[1]}'"
    elif body.startswith("resolution_ambiguity_"):
        text = f"resolution rules are ambiguous ({body.rsplit('_', 1)[1]})"
    elif body.startswith("category_blocked_"):
        text = f"category '{body.rsplit('_', 1)[1]}' is excluded"
    elif body.startswith("market_type_blocked_"):
        text = f"market type '{body.rsplit('_', 1)[1]}' is excluded"
    elif body.startswith("market_status_"):
        text = f"market is not open ({body.split('_', 2)[2]})"
    elif body.startswith("exchange_state_"):
        text = f"exchange has trading paused ({body.split('_', 2)[2]})"
    elif body.startswith("book_state_"):
        text = f"order book is not open ({body.split('_', 2)[2]})"
    elif body.startswith("paused_"):
        text = f"trading is paused ({body.split('_', 1)[1].replace('_', ' ')})"
    elif body.startswith("trading_disabled"):
        text = "trading is switched off"
    else:
        text = PLAIN_REASONS.get(body, body.replace("_", " "))
    return f"{tier} trade type: {text}" if tier else text


PLAIN_REASONS = {
    "price_out_of_range": "price outside this trade type's range",
    "event_not_already_occurred": "the outcome has not already happened",
    "resolution_criteria_not_read": "resolution rules were not confirmed",
    "no_two_sided_quote": "no usable bid and ask",
    "price_above_cap": "price above the 97 cent maximum",
    "price_below_floor": "price below the minimum we buy at",
    "no_tier_for_price": "price fits no trade type",
    "closes_too_soon": "market closes too soon",
    "closes_too_far": "market closes too far out",
    "already_holding": "already holding this market",
    "open_order_exists": "an order is already open on this market",
    "maintenance_window": "exchange maintenance window",
    "equity_below_minimum": "account too small to trade",
    "max_open_positions": "already at the maximum number of positions",
    "max_orders_per_day": "daily order limit reached",
    "size_below_minimum": "affordable size is under one contract",
    "exposure_cap": "would exceed the exposure limit",
    "reserved_cap": "too much cash already tied up in open orders",
    "insufficient_displayed_depth": "not enough volume on the order book",
    "open_interest_low": "market too thinly traded",
    "qty_must_be_positive": "quantity must be at least 1",
    "cost_above_max_trade": "costs more than the per-trade limit",
    "below_bankroll_floor": "would leave too little cash",
    "insufficient_position": "not holding enough to sell",
    "invalid_price": "invalid price",
    "market_not_found": "market not found",
    "no_quote": "no live quote",
    "decision_not_go": "risk checks did not pass",
    "unresolved_pending_orders": "an earlier order is still unresolved",
    "orders_lock_busy": "another order was being placed",
    "duplicate_idempotency_key": "identical order already exists",
    "duplicate_run_slug_intent": "already ordered this market in this run",
    "post_only_would_cross": "our price would have crossed the spread (maker-only order)",
    "no_immediate_fill": "no immediate fill available",
    "insufficient_liquidity": "not enough liquidity",
    "portfolio_target_reached": "already at the invested target, waiting for positions to settle",
    "game_imminent": "the game starts within the hour or is in progress",
    "spread_too_wide": "bid/ask gap too wide",
    "category_share_cap": "too many positions already in this category",
    "event_cap": "already have a position on this event",
}


def reasons_text(codes: list[str] | None) -> str:
    if not codes:
        return ""
    seen: list[str] = []
    for c in codes:
        w = reason_word(str(c))
        if w not in seen:
            seen.append(w)
    return "; ".join(seen)


def _label(item: dict) -> str:
    t = item.get("title") or item.get("question") or item.get("slug") or "?"
    q = item.get("question")
    if q and item.get("title") and t != q:
        return f"{q}: {t}"
    return str(t)


