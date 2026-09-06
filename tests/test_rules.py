from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pm.models import Account, ControlState, MarketInfo, Quote
from pm.rules import decide_favorite, eligible_side, score_pick, Pick


def test_score_prefers_sooner_cheaper_liquid(cfg):
    base = score_pick(cfg, Decimal("0.80"), Decimal("0.01"), Decimal("2"), Decimal("2000"))
    assert score_pick(cfg, Decimal("0.80"), Decimal("0.01"), Decimal("1"), Decimal("2000")) > base
    assert score_pick(cfg, Decimal("0.70"), Decimal("0.01"), Decimal("2"), Decimal("2000")) > base
    assert score_pick(cfg, Decimal("0.80"), Decimal("0.01"), Decimal("2"), Decimal("500")) < base
    assert score_pick(cfg, Decimal("0.80"), Decimal("0.03"), Decimal("2"), Decimal("2000")) < base


def test_eligible_side(cfg):
    assert eligible_side(cfg, Decimal("0.79"), Decimal("0.80")) == ("YES", Decimal("0.80"))
    assert eligible_side(cfg, Decimal("0.20"), Decimal("0.21")) == ("NO", Decimal("0.80"))
    assert eligible_side(cfg, Decimal("0.49"), Decimal("0.51")) is None
    assert eligible_side(cfg, Decimal("0.95"), Decimal("0.96")) is None


def _setup(cfg):
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    market = MarketInfo(slug="m", question="q", title="t", category="politics", end_date=now + timedelta(days=2), status="MARKET_STATUS_OPEN")
    quote = Quote(yes_bid=Decimal("0.79"), yes_ask=Decimal("0.80"), bids=[(Decimal("0.79"), Decimal("500"))], asks=[(Decimal("0.80"), Decimal("500"))], open_interest=Decimal("3000"), state="MARKET_STATE_OPEN")
    pick = Pick(market, "YES", Decimal("0.80"), Decimal("0.01"), Decimal("2"), Decimal("3000"), Decimal("0.01"))
    account = Account(cash=Decimal("10"), equity=Decimal("10"), exposure=Decimal("0"), reserved=Decimal("0"), open_positions=0, orders_today=0)
    control = ControlState(trading_enabled=True, paused_until=None, pause_reason="", kill_reason="", daily_realized_pnl=Decimal("0"), recent_losses=0, last_loss_at=None, in_maintenance=False)
    return now, market, quote, pick, account, control


def test_favorite_go_and_size(cfg):
    now, market, quote, pick, account, control = _setup(cfg)
    d = decide_favorite(cfg, pick, quote, account, control, now, {}, {})
    assert d.go, d.nogo_reasons
    assert d.tier == "favorite"
    assert d.proposed_price == Decimal("0.79")
    assert d.proposed_qty == 2
    assert d.expires_at <= now + timedelta(hours=3)


def test_favorite_target_and_caps(cfg):
    now, market, quote, pick, account, control = _setup(cfg)
    account.exposure = Decimal("5")
    assert "portfolio_target_reached" in decide_favorite(cfg, pick, quote, account, control, now, {}, {}).nogo_reasons
    account.exposure = Decimal("0")
    assert "event_cap" in decide_favorite(cfg, pick, quote, account, control, now, {}, {"q": 1}).nogo_reasons
    assert "category_share_cap" in decide_favorite(cfg, pick, quote, account, control, now, {"politics": 5}, {}).nogo_reasons
    account.held_slugs = {"m"}
    assert "already_holding" in decide_favorite(cfg, pick, quote, account, control, now, {}, {}).nogo_reasons


def test_favorite_game_freeze(cfg):
    now, market, quote, pick, account, control = _setup(cfg)
    market.category, market.market_type = "sports", "football_team_full_game_winner"
    market.game_start_time = now + timedelta(minutes=20)
    assert "game_imminent" in decide_favorite(cfg, pick, quote, account, control, now, {}, {}).nogo_reasons
    market.game_start_time = now + timedelta(hours=2)
    d = decide_favorite(cfg, pick, quote, account, control, now, {}, {})
    assert d.go and d.expires_at <= market.game_start_time
