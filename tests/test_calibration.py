from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pm.calibrate import bias_for, price_band, recompute
from pm.db import insert
from pm.models import Account, ControlState, MarketInfo, Quote
from pm.rules import Pick, decide_favorite
from pm.util import dumps, iso, now_utc


def _seed(conn, n_won, n_lost, category="sports", price="0.80"):
    for i in range(n_won + n_lost):
        slug = f"m{i}"
        won = i < n_won
        conn.execute("INSERT INTO markets(slug, category, settlement_price, question) VALUES (?, ?, ?, ?)", (slug, category, "1" if won else "0", slug))
        insert(conn, "candidates", {"run_id": "r", "slug": slug, "strategy": "favorites", "side": "YES", "scan_score": "0", "rank": 1, "researched": 0, "features_json": dumps({"yes_bid": str(Decimal(price) - Decimal("0.01")), "yes_ask": price}), "created_at": iso(now_utc())})


def test_price_band(cfg):
    assert price_band(cfg, Decimal("0.70")) == "0.65-0.75"
    assert price_band(cfg, Decimal("0.90")) == "0.85-0.91"


def test_recompute_shrinks_toward_prior(conn, cfg):
    _seed(conn, 36, 4)
    rows = recompute(conn, cfg)
    all_row = [r for r in rows if r["category"] == "all" and r["price_band"] == "all"][0]
    assert all_row["n"] == 40
    assert Decimal(all_row["mean_residual"]) == Decimal("0.1")
    est = Decimal(all_row["bias_est"])
    assert Decimal("0.025") < est < Decimal("0.1")
    b, n, src = bias_for(conn, cfg, "sports", Decimal("0.80"))
    assert n == 40 and src == "sports/0.75-0.85" and b == est


def test_bias_falls_back_to_prior_when_thin(conn, cfg):
    _seed(conn, 5, 5)
    recompute(conn, cfg)
    b, n, src = bias_for(conn, cfg, "politics", Decimal("0.70"))
    assert src == "prior" and b == cfg.favorites.bias


def test_negative_bias_bucket(conn, cfg):
    _seed(conn, 10, 30, category="culture")
    rows = recompute(conn, cfg)
    row = [r for r in rows if r["category"] == "culture" and r["price_band"] == "0.75-0.85"][0]
    assert Decimal(row["bias_est"]) < 0


def _decision_setup(total):
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(slug="k", question="q", category="politics", end_date=now + timedelta(days=2), status="MARKET_STATUS_OPEN")
    quote = Quote(yes_bid=Decimal("0.79"), yes_ask=Decimal("0.80"), bids=[(Decimal("0.79"), Decimal("5000"))], asks=[(Decimal("0.80"), Decimal("5000"))], open_interest=Decimal("3000"), state="MARKET_STATE_OPEN")
    pick = Pick(market, "YES", Decimal("0.80"), Decimal("0.01"), Decimal("2"), Decimal("3000"), Decimal("0.01"))
    account = Account(cash=total, equity=total, exposure=Decimal("0"), reserved=Decimal("0"), open_positions=0, orders_today=0)
    control = ControlState(trading_enabled=True, paused_until=None, pause_reason="", kill_reason="", daily_realized_pnl=Decimal("0"), recent_losses=0, last_loss_at=None, in_maintenance=False)
    return now, pick, quote, account, control


def test_kelly_switch(cfg):
    now, pick, quote, account, control = _decision_setup(Decimal("10"))
    small = decide_favorite(cfg, pick, quote, account, control, now, {}, {})
    assert small.go and small.proposed_price * small.proposed_qty <= Decimal("2.00")
    now, pick, quote, account, control = _decision_setup(Decimal("200"))
    big = decide_favorite(cfg, pick, quote, account, control, now, {}, {})
    kelly_dollars = cfg.sizing.kelly_multiplier * (cfg.favorites.bias / (Decimal("1") - Decimal("0.79"))) * Decimal("200")
    assert big.go
    assert big.proposed_price * big.proposed_qty <= kelly_dollars
    assert big.proposed_price * big.proposed_qty < Decimal("0.20") * Decimal("200")
