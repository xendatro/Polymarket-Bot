from datetime import timedelta
from decimal import Decimal

from pm.broker import BrokerTimeout
from pm.db import all_rows, one, scalar
from pm.execution import place_order
from pm.models import Decision
from pm.paper import PaperBroker
from pm.scan import upsert_market
from pm.util import now_utc


def _decision(price="0.91", qty=2, post_only=True, side="YES"):
    intent = "ORDER_INTENT_BUY_LONG" if side == "YES" else "ORDER_INTENT_BUY_SHORT"
    return Decision(slug="test-market", source="test", go=True, nogo_reasons=[], side=side, intent=intent, proposed_price=Decimal(price), proposed_qty=qty, order_type="limit", tif="GTD", post_only=post_only, expires_at=now_utc() + timedelta(hours=3), rationale="test")


def _market(fake_client, conn):
    return upsert_market(conn, fake_client.get_market("test-market"), now_utc())


def test_paper_resting_order_and_maker_fill(tmp_settings, cfg, conn, fake_client):
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    res = place_order(tmp_settings, cfg, conn, broker, _decision(price="0.91", qty=2), market, "run1", "test")
    assert res.status == "open", res.reason
    assert scalar(conn, "SELECT COUNT(*) FROM orders WHERE status = 'open'") == 1
    assert broker.poll() == []
    fake_client.asks = [("0.90", "40"), ("0.93", "100")]
    assert broker.poll() == []
    events = broker.poll()
    assert events and events[0]["type"] == "fill"
    row = one(conn, "SELECT * FROM orders WHERE order_id = ?", (res.order_id,))
    assert row["status"] == "filled"
    assert int(row["filled_qty"]) == 2
    pos = one(conn, "SELECT * FROM positions WHERE slug = 'test-market' AND side = 'YES'")
    assert int(pos["qty"]) == 2
    cash = broker.cash()
    assert Decimal("8.17") < cash < Decimal("10")
    fills = all_rows(conn, "SELECT * FROM fills")
    assert len(fills) == 1 and fills[0]["liquidity"] == "maker"


def test_paper_post_only_cross_rejected(tmp_settings, cfg, conn, fake_client):
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    res = place_order(tmp_settings, cfg, conn, broker, _decision(price="0.95", qty=1), market, "run2", "test")
    assert res.status == "rejected"
    assert res.reason == "post_only_would_cross"


def test_paper_taker_fill_pays_fee(tmp_settings, cfg, conn, fake_client):
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    res = place_order(tmp_settings, cfg, conn, broker, _decision(price="0.93", qty=3, post_only=False), market, "run3", "test")
    assert res.status == "filled", res.reason
    fees = Decimal(str(scalar(conn, "SELECT SUM(CAST(fee AS REAL)) FROM fills")))
    assert fees > 0
    assert abs(broker.cash() - (Decimal("10") - Decimal("0.92") * 3 - fees)) < Decimal("0.0001")


def test_idempotency_and_run_uniqueness(tmp_settings, cfg, conn, fake_client):
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    d = _decision(price="0.90", qty=1)
    first = place_order(tmp_settings, cfg, conn, broker, d, market, "run4", "test")
    second = place_order(tmp_settings, cfg, conn, broker, d, market, "run4", "test")
    assert second.duplicate and second.order_id == first.order_id
    third = place_order(tmp_settings, cfg, conn, broker, _decision(price="0.89", qty=1), market, "run4", "test")
    assert third.duplicate
    assert scalar(conn, "SELECT COUNT(*) FROM orders") == 1


def test_timeout_leaves_unknown_and_blocks_new_orders(tmp_settings, cfg, conn, fake_client):
    class FlakyBroker(PaperBroker):
        def create(self, req):
            raise BrokerTimeout("simulated")

    broker = FlakyBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    res = place_order(tmp_settings, cfg, conn, broker, _decision(price="0.90", qty=1), market, "run5", "test")
    assert res.status == "unknown"
    good = PaperBroker(tmp_settings, cfg, conn, fake_client)
    again = place_order(tmp_settings, cfg, conn, good, _decision(price="0.89", qty=1), market, "run6", "test")
    assert again.status == "rejected" and again.reason == "unresolved_pending_orders"


def test_dry_run_places_nothing(tmp_settings, cfg, conn, fake_client):
    from dataclasses import replace

    s = replace(tmp_settings, dry_run=True)
    broker = PaperBroker(s, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    res = place_order(s, cfg, conn, broker, _decision(), market, "run7", "test")
    assert res.status == "dry_run"
    assert scalar(conn, "SELECT COUNT(*) FROM orders") == 0


def test_expiry_via_poll(tmp_settings, cfg, conn, fake_client):
    broker = PaperBroker(tmp_settings, cfg, conn, fake_client)
    market = _market(fake_client, conn)
    d = _decision(price="0.90", qty=1)
    d.expires_at = now_utc() - timedelta(minutes=1)
    res = place_order(tmp_settings, cfg, conn, broker, d, market, "run8", "test")
    assert res.status == "open"
    events = broker.poll()
    assert events and events[0]["type"] == "expired"
    assert one(conn, "SELECT status FROM orders WHERE order_id = ?", (res.order_id,))["status"] == "expired"
