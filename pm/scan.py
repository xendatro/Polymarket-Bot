from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from pm.client import PMClient, book_summary
from pm.config import Config
from pm.db import insert, one, upsert
from pm.models import MarketInfo, Quote
from pm.util import ONE, ZERO, D, dstr, dumps, iso, now_utc


def upsert_market(conn: sqlite3.Connection, m: dict, now: datetime) -> MarketInfo:
    info = MarketInfo.from_api(m)
    existing = one(conn, "SELECT first_seen_at, no_trade_flag, no_trade_reason FROM markets WHERE slug = ?", (info.slug,))
    row = {
        "slug": info.slug,
        "market_id": info.market_id,
        "event_slug": m.get("eventSlug") or (m.get("marketMetadata") or {}).get("eventSlug"),
        "question": info.question,
        "title": info.title,
        "description": info.description,
        "category": info.category,
        "market_type": info.market_type,
        "start_date": m.get("startDate"),
        "end_date": iso(info.end_date),
        "game_start_time": iso(info.game_start_time),
        "status": info.status,
        "active": 1 if info.active else 0,
        "closed": 1 if info.closed else 0,
        "tick_size": dstr(info.tick),
        "fee_coefficient": dstr(info.fee_coef),
        "min_qty": dstr(info.min_qty),
        "first_seen_at": existing["first_seen_at"] if existing and existing["first_seen_at"] else iso(now),
        "last_seen_at": iso(now),
        "raw_json": dumps({k: v for k, v in m.items() if k not in ("marketSides", "image")}),
    }
    upsert(conn, "markets", row, ["slug"])
    return info


def record_snapshot(conn: sqlite3.Connection, slug: str, quote: Quote, source: str, now: datetime | None = None) -> int:
    now = now or now_utc()
    sid = insert(conn, "market_snapshots", {
        "slug": slug,
        "ts": iso(now),
        "yes_bid": dstr(quote.yes_bid),
        "yes_ask": dstr(quote.yes_ask),
        "yes_bid_size": dstr(quote.yes_bid_size),
        "yes_ask_size": dstr(quote.yes_ask_size),
        "last_trade": dstr(quote.last_trade),
        "open_interest": dstr(quote.open_interest),
        "shares_traded": None,
        "book_json": dumps({"bids": [[dstr(p), dstr(q)] for p, q in quote.bids], "asks": [[dstr(p), dstr(q)] for p, q in quote.asks]}),
        "source": source,
    })
    quote.snapshot_id = sid
    return sid


def fetch_quote(client: PMClient, slug: str, with_bbo: bool = True) -> Quote | None:
    book = client.get_book(slug)
    if book is None:
        return None
    s = book_summary(book)
    q = Quote(yes_bid=s["yes_bid"], yes_ask=s["yes_ask"], yes_bid_size=s["yes_bid_size"], yes_ask_size=s["yes_ask_size"], bids=s["bids"], asks=s["asks"], state=s.get("state") or "")
    stats = s.get("stats") or {}
    q.last_trade = D(stats.get("lastTradePx")) if stats else None
    if with_bbo:
        bbo = client.get_bbo(slug)
        if bbo:
            q.open_interest = D(bbo.get("openInterest"))
            q.last_trade = D(bbo.get("lastTradePx")) or q.last_trade
            if q.yes_bid is None:
                q.yes_bid = D(bbo.get("bestBid"))
            if q.yes_ask is None:
                q.yes_ask = D(bbo.get("bestAsk"))
    return q


def latest_quote_from_db(conn: sqlite3.Connection, slug: str) -> Quote | None:
    r = one(conn, "SELECT * FROM market_snapshots WHERE slug = ? ORDER BY snapshot_id DESC LIMIT 1", (slug,))
    if r is None:
        return None
    from pm.util import loads
    book = loads(r["book_json"], {}) or {}
    q = Quote(yes_bid=D(r["yes_bid"]), yes_ask=D(r["yes_ask"]), yes_bid_size=D(r["yes_bid_size"]), yes_ask_size=D(r["yes_ask_size"]), bids=[(D(p), D(s)) for p, s in book.get("bids", [])], asks=[(D(p), D(s)) for p, s in book.get("asks", [])], open_interest=D(r["open_interest"]), last_trade=D(r["last_trade"]))
    q.snapshot_id = r["snapshot_id"]
    return q


def market_from_db(conn: sqlite3.Connection, slug: str) -> MarketInfo | None:
    r = one(conn, "SELECT * FROM markets WHERE slug = ?", (slug,))
    if r is None:
        return None
    from pm.util import loads, parse_iso
    return MarketInfo(slug=slug, question=r["question"] or "", title=r["title"] or "", description=r["description"] or "", category=r["category"] or "", market_type=r["market_type"] or "", end_date=parse_iso(r["end_date"]), game_start_time=parse_iso(r["game_start_time"]) if "game_start_time" in r.keys() else None, tick=D(r["tick_size"], Decimal("0.01")), fee_coef=D(r["fee_coefficient"], Decimal("0.06")), min_qty=D(r["min_qty"], Decimal("1")), status=r["status"] or "", closed=bool(r["closed"]), active=bool(r["active"]), market_id=r["market_id"] or "", raw=loads(r["raw_json"], {}) or {})
