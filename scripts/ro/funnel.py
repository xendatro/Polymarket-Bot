import sys
from datetime import timedelta
from decimal import Decimal

from pm.client import PMClient, market_quotes
from pm.models import MarketInfo
from pm.rules import eligible_side, score_pick
from pm.util import D, ONE, ZERO, dstr, iso, now_utc
from scripts.common import boot, out


def main() -> None:
    settings, cfg, _, conn = boot(readonly=True)
    client = PMClient()
    now = now_utc()
    f = cfg.favorites
    seen = {}
    for cat in cfg.scan.categories:
        if cat in cfg.no_trade_categories:
            continue
        for m in client.list_markets(max_items=cfg.scan.max_markets, active=True, closed=False, categories=[cat], endDateMin=iso(now), endDateMax=iso(now + timedelta(days=float(max(f.max_days, f.fetch_window_days))))):
            seen[m["slug"]] = m
    stats = {"fetched": len(seen), "open": 0, "two_sided_quote": 0, "spread_ok": 0, "settles_in_window": 0, "not_frozen": 0, "favorite_priced": 0, "liquid": 0}
    rows = []
    for slug, m in seen.items():
        info = MarketInfo.from_api(m)
        if info.status not in ("", "MARKET_STATUS_OPEN") or info.closed or m.get("hidden"):
            continue
        stats["open"] += 1
        bid, ask = market_quotes(m)
        if bid is None or ask is None or bid <= ZERO or ask >= ONE:
            continue
        stats["two_sided_quote"] += 1
        spread = ask - bid
        if spread > f.max_spread:
            continue
        stats["spread_ok"] += 1
        et = info.event_time
        if et is None:
            continue
        hours = Decimal((et - now).total_seconds()) / 3600
        if hours < f.min_hours or hours > f.max_days * 24:
            continue
        stats["settles_in_window"] += 1
        if info.frozen_at(now, cfg.exits.freeze_before_game_minutes):
            continue
        stats["not_frozen"] += 1
        es = eligible_side(cfg, bid, ask)
        if es is None:
            continue
        stats["favorite_priced"] += 1
        rows.append((info, es[0], es[1], spread, hours / 24))
    checked = []
    for info, side, price, spread, days in rows[: f.check_top_n]:
        oi = D((client.get_bbo(info.slug) or {}).get("openInterest"))
        ok = oi is not None and oi >= f.min_open_interest
        if ok:
            stats["liquid"] += 1
        checked.append({"slug": info.slug, "question": (info.question or "")[:50], "title": (info.title or "")[:24], "category": info.category, "side": side, "price": dstr(price), "spread": dstr(spread), "days": dstr(days, 1), "open_interest": dstr(oi, 0), "liquid": ok, "score": dstr(score_pick(cfg, price, spread, days, oi), 4) if ok else None})
    checked.sort(key=lambda r: (r["liquid"], float(r["score"] or -9)), reverse=True)
    out({"funnel": stats, "candidates": checked[:25]})


if __name__ == "__main__":
    main()
