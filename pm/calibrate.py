from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from pm.client import PMClient
from pm.config import Config
from pm.db import all_rows, one, upsert
from pm.scan import market_from_db, upsert_market
from pm.util import ONE, ZERO, D, dstr, iso, loads, now_utc


def price_band(cfg: Config, price: Decimal) -> str:
    edges = cfg.calibration.price_bands
    for lo, hi in zip(edges, edges[1:]):
        if lo <= price < hi:
            return f"{dstr(lo, 2)}-{dstr(hi, 2)}"
    return f"{dstr(edges[-2], 2)}-{dstr(edges[-1], 2)}"


def refresh_observed_settlements(conn: sqlite3.Connection, client: PMClient, now: datetime | None = None, max_checks: int = 80) -> int:
    now = now or now_utc()
    rows = all_rows(conn, """
        SELECT DISTINCT c.slug FROM candidates c
        JOIN markets m ON m.slug = c.slug
        WHERE c.strategy = 'favorites' AND m.settlement_price IS NULL
          AND (m.last_seen_at IS NULL OR m.last_seen_at <= ?)
        LIMIT ?
    """, (iso(now - timedelta(hours=6)), max_checks))
    n = 0
    for r in rows:
        mk = market_from_db(conn, r["slug"])
        if mk is None or mk.event_time is None or mk.event_time > now - timedelta(hours=2):
            continue
        m = client.get_market(r["slug"])
        if m is None:
            continue
        info = upsert_market(conn, m, now)
        if not info.resolved:
            continue
        price = client.get_settlement(r["slug"])
        if price is None:
            for side in m.get("marketSides") or []:
                if side.get("long") is True and side.get("price") is not None:
                    price = D(side.get("price"))
        if price is None:
            continue
        conn.execute("UPDATE markets SET resolved_outcome = ?, settlement_price = ?, settled_at = ? WHERE slug = ?", ("YES" if price >= Decimal("0.5") else "NO", dstr(price), iso(now), r["slug"]))
        n += 1
    return n


def recompute(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> list[dict]:
    now = now or now_utc()
    rows = all_rows(conn, """
        SELECT c.slug, c.side, c.features_json, m.category, m.settlement_price
        FROM candidates c JOIN markets m ON m.slug = c.slug
        WHERE c.strategy = 'favorites' AND m.settlement_price IS NOT NULL
          AND c.candidate_id = (SELECT MIN(candidate_id) FROM candidates c2 WHERE c2.slug = c.slug AND c2.strategy = 'favorites')
    """)
    buckets: dict[tuple[str, str], list[Decimal]] = {}
    for r in rows:
        f = loads(r["features_json"], {}) or {}
        yes_bid, yes_ask = D(f.get("yes_bid")), D(f.get("yes_ask"))
        if yes_bid is None or yes_ask is None:
            continue
        side_price = yes_ask if r["side"] == "YES" else ONE - yes_bid
        settled_yes = (D(r["settlement_price"]) or ZERO) >= Decimal("0.5")
        won = ONE if ((r["side"] == "YES") == settled_yes) else ZERO
        resid = won - side_price
        for key in ((r["category"] or "other", price_band(cfg, side_price)), ("all", price_band(cfg, side_price)), ("all", "all"), (r["category"] or "other", "all")):
            buckets.setdefault(key, []).append(resid)
    out = []
    k = Decimal(cfg.calibration.prior_weight)
    prior = cfg.favorites.bias
    for (cat, band), vals in buckets.items():
        n = len(vals)
        mean = sum(vals, ZERO) / n
        est = (k * prior + n * mean) / (k + n)
        row = {"category": cat, "price_band": band, "n": n, "mean_residual": dstr(mean, 4), "bias_est": dstr(est, 4), "updated_at": iso(now)}
        upsert(conn, "calibration", row, ["category", "price_band"])
        out.append(row)
    return out


def bias_for(conn: sqlite3.Connection, cfg: Config, category: str | None, price: Decimal) -> tuple[Decimal, int, str]:
    band = price_band(cfg, price)
    for cat, b in ((category or "other", band), ("all", band), (category or "other", "all"), ("all", "all")):
        r = one(conn, "SELECT n, bias_est FROM calibration WHERE category = ? AND price_band = ?", (cat, b))
        if r is not None and int(r["n"]) >= cfg.calibration.min_n_to_use:
            return D(r["bias_est"]) or ZERO, int(r["n"]), f"{cat}/{b}"
    return cfg.favorites.bias, 0, "prior"
