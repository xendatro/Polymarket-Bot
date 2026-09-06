from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Protocol

from polymarket_us.errors import APIConnectionError, APITimeoutError

from pm.client import PMClient
from pm.config import Config, Settings
from pm.db import get_control
from pm.models import STATE_MAP, BrokerOrder, OrderRequest
from pm.util import ZERO, D


class BrokerTimeout(Exception):
    pass


class Broker(Protocol):
    mode: str

    def preview(self, req: OrderRequest) -> dict: ...
    def create(self, req: OrderRequest) -> BrokerOrder: ...
    def cancel(self, exchange_order_id: str, slug: str) -> None: ...
    def cancel_all(self) -> list[str]: ...
    def get_order(self, exchange_order_id: str) -> BrokerOrder | None: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def positions(self) -> dict[str, dict]: ...
    def balances(self) -> dict: ...


def normalize_order(o: dict) -> BrokerOrder:
    state = o.get("state") or ""
    status = STATE_MAP.get(state, "unknown")
    filled = int(D(o.get("cumQuantity"), ZERO) or 0)
    avg = D(o.get("avgPx"))
    fees = D(o.get("commissionNotionalTotalCollected"), ZERO) or ZERO
    return BrokerOrder(exchange_order_id=o.get("id"), status=status, filled_qty=filled, avg_price=avg, fees=fees, raw=o)


class LiveBroker:
    mode = "live"

    def __init__(self, settings: Settings, cfg: Config, conn: sqlite3.Connection, client: PMClient | None = None):
        if not settings.is_live:
            raise RuntimeError("LiveBroker requires PM_MODE=live")
        if not settings.key_id or not settings.secret_key:
            raise RuntimeError("LiveBroker requires POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY")
        self.armed = get_control(conn, "trading_enabled", "0") == "1"
        self.client = client or PMClient(settings)
        self.cfg = cfg
        self.conn = conn

    def preview(self, req: OrderRequest) -> dict:
        return self.client.preview_order(req.api_params())

    def create(self, req: OrderRequest) -> BrokerOrder:
        if get_control(self.conn, "trading_enabled", "0") != "1":
            raise RuntimeError("live trading is not armed; use /arm in Discord or python -m scripts.halt arm")
        try:
            res = self.client.create_order(req.api_params())
        except (APITimeoutError, APIConnectionError) as e:
            raise BrokerTimeout(str(e)) from e
        oid = (res or {}).get("id")
        execs = (res or {}).get("executions") or []
        out = BrokerOrder(exchange_order_id=oid, status="submitted", filled_qty=0, avg_price=None, fees=ZERO, raw=res or {})
        for ex in execs:
            if ex.get("type") in ("EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"):
                out.fills.append({"id": ex.get("tradeId") or ex.get("id"), "price": D(ex.get("lastPx")), "qty": int(D(ex.get("lastShares"), ZERO) or 0), "fee": D(ex.get("commissionNotionalCollected"), ZERO), "aggressor": ex.get("aggressor"), "ts": ex.get("transactTime")})
            if ex.get("type") == "EXECUTION_TYPE_REJECTED":
                out.status = "rejected"
                out.reject_reason = ex.get("orderRejectReason") or ex.get("text") or "rejected"
            if ex.get("order"):
                n = normalize_order(ex["order"])
                out.status = n.status if out.status != "rejected" else out.status
                out.filled_qty = n.filled_qty
                out.avg_price = n.avg_price
                out.fees = n.fees
        if oid and out.status == "submitted":
            fetched = self.get_order(oid)
            if fetched is not None:
                fetched.fills = out.fills
                return fetched
        return out

    def cancel(self, exchange_order_id: str, slug: str) -> None:
        self.client.cancel_order(exchange_order_id, slug)

    def cancel_all(self) -> list[str]:
        return self.client.cancel_all()

    def get_order(self, exchange_order_id: str) -> BrokerOrder | None:
        o = self.client.get_order(exchange_order_id)
        return normalize_order(o) if o else None

    def open_orders(self) -> list[BrokerOrder]:
        return [normalize_order(o) for o in self.client.open_orders()]

    def positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for slug, p in self.client.positions().items():
            net = D(p.get("netPositionDecimal") or p.get("netPosition"), ZERO) or ZERO
            if net == 0:
                continue
            side = "YES" if net > 0 else "NO"
            qty = int(abs(net))
            cost = D(p.get("cost"), ZERO) or ZERO
            out[slug] = {"side": side, "qty": qty, "cost_basis": abs(cost), "avg_cost": (abs(cost) / qty) if qty else None, "realized": D(p.get("realized"), ZERO), "expired": bool(p.get("expired")), "raw": p}
        return out

    def balances(self) -> dict:
        b = self.client.balances()
        return {
            "cash": D(b.get("currentBalance"), ZERO),
            "buying_power": D(b.get("buyingPower")),
            "positions_mark": D(b.get("assetNotional"), ZERO),
            "open_orders_notional": D(b.get("openOrders"), ZERO),
            "raw": b,
        }

    def close_position(self, slug: str) -> dict:
        return self.client.close_position(slug)


def make_broker(settings: Settings, cfg: Config, conn: sqlite3.Connection, client: PMClient):
    if settings.is_live:
        return LiveBroker(settings, cfg, conn, client)
    from pm.paper import PaperBroker
    return PaperBroker(settings, cfg, conn, client)
