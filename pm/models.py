from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pm.util import D, parse_iso

INTENT_BY_SIDE = {"YES": "ORDER_INTENT_BUY_LONG", "NO": "ORDER_INTENT_BUY_SHORT"}
EXIT_INTENT_BY_SIDE = {"YES": "ORDER_INTENT_SELL_LONG", "NO": "ORDER_INTENT_SELL_SHORT"}
SIDE_BY_INTENT = {v: k for k, v in INTENT_BY_SIDE.items()}
SIDE_BY_INTENT.update({v: k for k, v in EXIT_INTENT_BY_SIDE.items()})

TIF_API = {
    "GTC": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "GTD": "TIME_IN_FORCE_GOOD_TILL_DATE",
    "IOC": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "FOK": "TIME_IN_FORCE_FILL_OR_KILL",
}
ORDER_TYPE_API = {"limit": "ORDER_TYPE_LIMIT", "market": "ORDER_TYPE_MARKET"}

STATE_MAP = {
    "ORDER_STATE_NEW": "open",
    "ORDER_STATE_PENDING_NEW": "open",
    "ORDER_STATE_PENDING_RISK": "open",
    "ORDER_STATE_PENDING_REPLACE": "open",
    "ORDER_STATE_PARTIALLY_FILLED": "partially_filled",
    "ORDER_STATE_FILLED": "filled",
    "ORDER_STATE_CANCELED": "cancelled",
    "ORDER_STATE_PENDING_CANCEL": "cancel_requested",
    "ORDER_STATE_REJECTED": "rejected",
    "ORDER_STATE_EXPIRED": "expired",
    "ORDER_STATE_REPLACED": "replaced",
}
TERMINAL_STATUSES = {"filled", "cancelled", "rejected", "expired", "replaced"}
LIVE_STATUSES = {"pending_submit", "submitted", "open", "partially_filled", "cancel_requested", "unknown"}


@dataclass
class MarketInfo:
    slug: str
    question: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    market_type: str = ""
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    tick: Decimal = Decimal("0.01")
    fee_coef: Decimal = Decimal("0.06")
    min_qty: Decimal = Decimal("1")
    status: str = ""
    closed: bool = False
    active: bool = True
    market_id: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, m: dict) -> "MarketInfo":
        return cls(
            slug=m.get("slug", ""),
            question=m.get("question") or "",
            title=m.get("title") or "",
            description=m.get("description") or "",
            category=m.get("category") or "",
            market_type=(m.get("marketType") or m.get("sportsMarketType") or ""),
            end_date=parse_iso(m.get("endDate")),
            game_start_time=parse_iso(m.get("gameStartTime")),
            tick=D(m.get("orderPriceMinTickSize"), Decimal("0.01")),
            fee_coef=D(m.get("feeCoefficient"), Decimal("0.06")),
            min_qty=D(m.get("minimumTradeQty"), Decimal("1")),
            status=m.get("status") or "",
            closed=bool(m.get("closed")),
            active=bool(m.get("active", True)),
            market_id=str(m.get("id") or ""),
            raw=m,
        )

    @property
    def resolved(self) -> bool:
        return self.closed or self.status in ("MARKET_STATUS_RESOLVED", "MARKET_STATUS_SETTLED")

    @property
    def is_game(self) -> bool:
        return self.game_start_time is not None and self.category == "sports" and self.market_type not in ("futures", "future", "")

    @property
    def slug_date(self) -> datetime | None:
        import re
        from datetime import timezone
        mt = re.search(r"(20\d\d)-(\d\d)-(\d\d)", self.slug or "")
        if not mt:
            return None
        try:
            return datetime(int(mt.group(1)), int(mt.group(2)), int(mt.group(3)), 23, 59, tzinfo=timezone.utc)
        except ValueError:
            return None

    @property
    def event_time(self) -> datetime | None:
        if self.is_game:
            return self.game_start_time
        sd = self.slug_date
        if sd is not None and (self.end_date is None or sd <= self.end_date):
            return sd
        return self.end_date

    def frozen_at(self, now: datetime, minutes_before: int = 60) -> bool:
        if not self.is_game or self.resolved:
            return False
        from datetime import timedelta
        return now >= self.game_start_time - timedelta(minutes=minutes_before)


@dataclass
class Quote:
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_size: Decimal | None = None
    yes_ask_size: Decimal | None = None
    bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    open_interest: Decimal | None = None
    last_trade: Decimal | None = None
    state: str = ""
    snapshot_id: int | None = None

    def side_prices(self, side: str) -> tuple[Decimal | None, Decimal | None]:
        if side == "YES":
            return self.yes_bid, self.yes_ask
        bid = (Decimal(1) - self.yes_ask) if self.yes_ask is not None else None
        ask = (Decimal(1) - self.yes_bid) if self.yes_bid is not None else None
        return bid, ask

    def displayed_depth(self) -> Decimal:
        return sum((q for _, q in self.bids), Decimal(0)) + sum((q for _, q in self.asks), Decimal(0))

    @property
    def mid(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


@dataclass
class Account:
    cash: Decimal
    equity: Decimal
    exposure: Decimal
    reserved: Decimal
    open_positions: int
    orders_today: int
    buying_power: Decimal | None = None
    held_slugs: set[str] = field(default_factory=set)
    open_order_slugs: set[str] = field(default_factory=set)
    positions_mark: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.equity

    @property
    def invested(self) -> Decimal:
        return self.exposure + self.reserved

    @property
    def invested_pct(self) -> Decimal:
        return (self.invested / self.equity) if self.equity > 0 else Decimal("0")


@dataclass
class ControlState:
    trading_enabled: bool
    paused_until: datetime | None
    pause_reason: str
    kill_reason: str
    daily_realized_pnl: Decimal
    recent_losses: int
    last_loss_at: datetime | None
    in_maintenance: bool


@dataclass
class OrderRequest:
    slug: str
    side: str
    intent: str
    order_type: str
    tif: str
    qty: int
    price: Decimal | None
    post_only: bool
    good_till: datetime | None
    client_ref: str
    tick: Decimal = Decimal("0.01")
    fee_coef: Decimal = Decimal("0.06")

    def api_params(self) -> dict:
        p: dict[str, Any] = {
            "marketSlug": self.slug,
            "intent": self.intent,
            "type": ORDER_TYPE_API[self.order_type],
            "quantity": int(self.qty),
            "tif": TIF_API[self.tif],
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        if self.order_type == "limit" and self.price is not None:
            p["price"] = {"value": format(self.price.normalize(), "f"), "currency": "USD"}
            p["participateDontInitiate"] = bool(self.post_only)
        if self.tif == "GTD" and self.good_till is not None:
            from pm.util import iso
            p["goodTillTime"] = iso(self.good_till)
        return p


@dataclass
class BrokerOrder:
    exchange_order_id: str | None
    status: str
    filled_qty: int
    avg_price: Decimal | None
    fees: Decimal
    raw: dict = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    reject_reason: str = ""


@dataclass
class Decision:
    slug: str
    source: str
    go: bool
    nogo_reasons: list[str]
    side: str | None = None
    intent: str | None = None
    p_model_raw: Decimal | None = None
    p_model_shrunk: Decimal | None = None
    p_market: Decimal | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    proposed_price: Decimal | None = None
    fee_per_contract: Decimal | None = None
    edge_net: Decimal | None = None
    kelly_full: Decimal | None = None
    kelly_used: Decimal | None = None
    tier: str | None = None
    proposed_qty: int = 0
    order_type: str = "limit"
    tif: str = "GTD"
    post_only: bool = True
    expires_at: datetime | None = None
    rationale: str = ""
    equity_at_decision: Decimal | None = None
    exposure_at_decision: Decimal | None = None
    note_id: int | None = None
    snapshot_id: int | None = None
    decision_id: int | None = None
    tick: Decimal = Decimal("0.01")
    fee_coef: Decimal = Decimal("0.06")

    def to_row(self, run_id: str | None, config_hash: str, created_at: str) -> dict:
        from pm.util import dstr, dumps, iso
        return {
            "run_id": run_id,
            "slug": self.slug,
            "note_id": self.note_id,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "side": self.side,
            "intent": self.intent,
            "p_model_raw": dstr(self.p_model_raw),
            "p_model_shrunk": dstr(self.p_model_shrunk),
            "p_market": dstr(self.p_market),
            "best_bid": dstr(self.best_bid),
            "best_ask": dstr(self.best_ask),
            "spread": dstr(self.spread),
            "proposed_price": dstr(self.proposed_price),
            "fee_per_contract": dstr(self.fee_per_contract, 6),
            "edge_net": dstr(self.edge_net),
            "kelly_full": dstr(self.kelly_full),
            "kelly_used": dstr(self.kelly_used),
            "tier": self.tier,
            "proposed_qty": self.proposed_qty,
            "order_type": self.order_type,
            "tif": self.tif,
            "expires_at": iso(self.expires_at),
            "go": 1 if self.go else 0,
            "nogo_reasons_json": dumps(self.nogo_reasons),
            "rationale": self.rationale,
            "equity_at_decision": dstr(self.equity_at_decision, 2),
            "exposure_at_decision": dstr(self.exposure_at_decision, 2),
            "config_hash": config_hash,
            "created_at": created_at,
        }
