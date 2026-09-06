import os

import pytest

os.environ.setdefault("PM_MODE", "paper")


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_MODE", "paper")
    monkeypatch.setenv("PM_DRY_RUN", "0")
    monkeypatch.setenv("PM_DATA_DIR", str(tmp_path))
    from pm.config import load_settings
    return load_settings()


@pytest.fixture
def cfg():
    from pm.config import load_config
    return load_config()[0]


@pytest.fixture
def conn(tmp_settings, cfg):
    from pm.db import connect, init_schema
    c = connect(tmp_settings.db_path)
    init_schema(c, "paper", cfg.paper.starting_cash)
    yield c
    c.close()


class FakeClient:
    def __init__(self, bids=None, asks=None, market=None):
        self.bids = bids or [("0.90", "50"), ("0.89", "100")]
        self.asks = asks or [("0.92", "40"), ("0.93", "100")]
        self.market = market or {"slug": "test-market", "question": "Test?", "title": "Test", "description": "Resolves YES if test.", "category": "politics", "marketType": "futures", "endDate": "2030-01-01T00:00:00Z", "status": "MARKET_STATUS_OPEN", "active": True, "closed": False, "orderPriceMinTickSize": 0.01, "feeCoefficient": 0.06, "minimumTradeQty": 1, "bestBidQuote": {"value": self.bids[0][0]}, "bestAskQuote": {"value": self.asks[0][0]}, "id": "1"}
        self.has_credentials = False

    def get_book(self, slug):
        return {"marketSlug": slug, "bids": [{"px": {"value": p, "currency": "USD"}, "qty": q} for p, q in self.bids], "offers": [{"px": {"value": p, "currency": "USD"}, "qty": q} for p, q in self.asks], "state": "MARKET_STATE_OPEN", "stats": {}}

    def get_bbo(self, slug):
        return {"bestBid": {"value": self.bids[0][0]}, "bestAsk": {"value": self.asks[0][0]}, "openInterest": "5000"}

    def get_market(self, slug):
        return dict(self.market, slug=slug)

    def get_settlement(self, slug):
        return None

    def price_history(self, slug, interval="INTERVAL_1W", fidelity=60):
        return []

    def server_clock_skew_seconds(self):
        return 0.0


@pytest.fixture
def fake_client():
    return FakeClient()
