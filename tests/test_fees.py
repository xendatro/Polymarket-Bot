from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pm.risk import breakeven_probability, fee_per_contract, in_maintenance_window, kelly_fraction, maker_rebate, taker_fee


def test_taker_fee_matches_schedule():
    assert taker_fee(Decimal("0.50"), 100) == Decimal("1.500000")
    assert taker_fee(Decimal("0.95"), 100) == Decimal("0.285000")
    assert fee_per_contract(Decimal("0.30")) == fee_per_contract(Decimal("0.70"))
    assert maker_rebate(Decimal("0.50"), 100) < taker_fee(Decimal("0.50"), 100)


def test_breakeven_and_kelly():
    assert breakeven_probability(Decimal("0.95")) > Decimal("0.95")
    assert kelly_fraction(Decimal("0.97"), Decimal("0.953")) > Decimal("0.3")
    assert kelly_fraction(Decimal("0.90"), Decimal("0.95")) == 0


def test_maintenance_window(cfg):
    thursday_3am_et = datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc)
    assert in_maintenance_window(cfg, thursday_3am_et)
    assert not in_maintenance_window(cfg, thursday_3am_et + timedelta(hours=5))
