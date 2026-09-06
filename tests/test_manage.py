from datetime import timedelta
from decimal import Decimal

from pm.db import one, scalar
from pm.execution import place_order
from pm.manage import exit_levels, manage_positions
from pm.models import Decision
from pm.paper import PaperBroker
from pm.scan import upsert_market
from pm.util import iso, now_utc


def _buy_and_fill(tmp_settings, cfg, conn, fake_client, price="0.60", qty=2):
    fake_client.bids = [("0.59", "100")]
    fake_client.asks = [("0.61", "100")]
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = upsert_market(conn, fake_client.get_market("test-market"), now_utc())
    d = Decision(slug="test-market", source="test", go=True, nogo_reasons=[], side="YES", intent="ORDER_INTENT_BUY_LONG", proposed_price=Decimal(price), proposed_qty=qty, order_type="limit", tif="GTD", post_only=False, expires_at=now_utc() + timedelta(hours=3), rationale="t")
    fake_client.asks = [(price, "100")]
    res = place_order(tmp_settings, cfg, conn, broker, d, market, "runX", "test")
    assert res.status == "filled", res.reason
    return broker, market


def _legacy(cfg):
    c = cfg.model_copy(deep=True)
    c.exits.take_profit_cents = Decimal("0.08")
    c.exits.stop_loss_enabled = True
    return c


def test_exit_levels(cfg):
    tp, sl = exit_levels(cfg, Decimal("0.60"))
    assert tp == cfg.exits.take_profit_price and sl is None
    tp2, sl2 = exit_levels(_legacy(cfg), Decimal("0.60"))
    assert tp2 == Decimal("0.68") and sl2 == Decimal("0.40")


def test_default_config_holds_through_drops(tmp_settings, cfg, conn, fake_client):
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    fake_client.bids = [("0.38", "100")]
    fake_client.asks = [("0.40", "100")]
    for _ in range(3):
        assert manage_positions(tmp_settings, cfg, conn, fake_client, broker) == []
    assert one(conn, "SELECT status FROM positions WHERE slug = 'test-market'")["status"] == "open"


def test_take_profit_sells(tmp_settings, cfg, conn, fake_client):
    cfg = _legacy(cfg)
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    cash_after_buy = broker.cash()
    fake_client.bids = [("0.70", "100")]
    fake_client.asks = [("0.72", "100")]
    events = manage_positions(tmp_settings, cfg, conn, fake_client, broker)
    assert events and events[0]["type"] == "take_profit"
    pos = one(conn, "SELECT * FROM positions WHERE slug = 'test-market' AND side = 'YES'")
    assert pos["status"] == "closed" and int(pos["qty"]) == 0
    assert broker.cash() > cash_after_buy
    assert Decimal(pos["realized_pnl"]) > 0


def test_stop_loss_sells(tmp_settings, cfg, conn, fake_client):
    cfg = _legacy(cfg)
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    fake_client.bids = [("0.38", "100")]
    fake_client.asks = [("0.40", "100")]
    first = manage_positions(tmp_settings, cfg, conn, fake_client, broker)
    assert first == []
    assert one(conn, "SELECT status FROM positions WHERE slug = 'test-market'")["status"] == "open"
    events = manage_positions(tmp_settings, cfg, conn, fake_client, broker)
    assert events and events[0]["type"] == "stop_loss"
    pos = one(conn, "SELECT * FROM positions WHERE slug = 'test-market' AND side = 'YES'")
    assert pos["status"] == "closed"
    assert Decimal(pos["realized_pnl"]) < 0


def test_no_action_in_between(tmp_settings, cfg, conn, fake_client):
    cfg = _legacy(cfg)
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    fake_client.bids = [("0.64", "100")]
    fake_client.asks = [("0.66", "100")]
    assert manage_positions(tmp_settings, cfg, conn, fake_client, broker) == []
    pos = one(conn, "SELECT * FROM positions WHERE slug = 'test-market'")
    assert pos["status"] == "open" and pos["take_profit_price"] == "0.68"


def test_frozen_during_game(tmp_settings, cfg, conn, fake_client):
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    conn.execute("UPDATE markets SET game_start_time = ?, category = 'sports', market_type = 'moneyline' WHERE slug = 'test-market'", (iso(now_utc() - timedelta(minutes=10)),))
    fake_client.bids = [("0.90", "100")]
    fake_client.asks = [("0.92", "100")]
    events = manage_positions(tmp_settings, cfg, conn, fake_client, broker)
    assert events and events[0]["type"] == "frozen"
    assert one(conn, "SELECT status FROM positions WHERE slug = 'test-market'")["status"] == "open"


def test_thin_book_blocks_selling(tmp_settings, cfg, conn, fake_client):
    cfg = _legacy(cfg)
    broker, market = _buy_and_fill(tmp_settings, cfg, conn, fake_client)
    fake_client.bids = [("0.30", "100")]
    fake_client.asks = [("0.70", "100")]
    events = manage_positions(tmp_settings, cfg, conn, fake_client, broker)
    assert events and events[0]["type"] == "thin_book"
    assert one(conn, "SELECT status FROM positions WHERE slug = 'test-market'")["status"] == "open"


def test_sports_futures_frozen_on_event_day(cfg):
    from pm.models import MarketInfo
    from pm.util import now_utc
    now = now_utc()
    m = MarketInfo(slug=f"tec-cup-final-{now.strftime('%Y-%m-%d')}-w-abc", category="sports", market_type="futures", end_date=now + timedelta(days=10))
    assert m.frozen_at(now, 60)
    later = MarketInfo(slug=f"tec-cup-final-{(now + timedelta(days=3)).strftime('%Y-%m-%d')}-w-abc", category="sports", market_type="futures", end_date=now + timedelta(days=10))
    assert not later.frozen_at(now, 60)
