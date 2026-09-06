from __future__ import annotations

import threading
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from polymarket_us import PolymarketUS
from polymarket_us.errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
    RateLimitError,
)

from pm.config import Settings
from pm.util import D, now_utc

GATEWAY = "https://gateway.polymarket.us"
PLACEHOLDER_LOW = Decimal("0.002")
PLACEHOLDER_HIGH = Decimal("0.998")


class RateLimiter:
    def __init__(self, per_second: float = 10.0):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_ok = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_ok:
                time.sleep(self.next_ok - now)
                now = time.monotonic()
            self.next_ok = now + self.interval


class PMClient:
    def __init__(self, settings: Settings | None = None, key_id: str | None = None, secret_key: str | None = None):
        if settings is not None:
            key_id = key_id or settings.key_id
            secret_key = secret_key or settings.secret_key
        self.sdk = PolymarketUS(key_id=key_id, secret_key=secret_key)
        self.http = httpx.Client(timeout=30.0, follow_redirects=True)
        self.limiter = RateLimiter(10.0)
        self.has_credentials = bool(key_id and secret_key)
        self.error_count = 0

    def _call(self, fn, *a, **kw):
        self.limiter.wait()
        try:
            out = fn(*a, **kw)
            self.error_count = 0
            return out
        except (APITimeoutError, APIConnectionError, RateLimitError):
            self.error_count += 1
            raise
        except APIStatusError as e:
            if getattr(e, "status_code", 0) and e.status_code >= 500:
                self.error_count += 1
            raise

    def list_markets(self, max_items: int = 500, page: int = 100, **params) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < max_items:
            q = dict(params)
            q["limit"] = min(page, max_items - len(out))
            q["offset"] = offset
            res = self._call(self.sdk.markets.list, q)
            batch = res.get("markets") or []
            out.extend(batch)
            if len(batch) < q["limit"]:
                break
            offset += len(batch)
        return out

    def list_events(self, max_items: int = 500, page: int = 100, **params) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < max_items:
            q = dict(params)
            q["limit"] = min(page, max_items - len(out))
            q["offset"] = offset
            res = self._call(self.sdk.events.list, q)
            batch = res.get("events") or []
            out.extend(batch)
            if len(batch) < q["limit"]:
                break
            offset += len(batch)
        return out

    def get_market(self, slug: str) -> dict | None:
        try:
            res = self._call(self.sdk.markets.retrieve_by_slug, slug)
        except NotFoundError:
            return None
        return res.get("market") if isinstance(res, dict) else None

    def get_book(self, slug: str) -> dict | None:
        try:
            res = self._call(self.sdk.markets.book, slug)
        except NotFoundError:
            return None
        return res.get("marketData") if isinstance(res, dict) else None

    def get_bbo(self, slug: str) -> dict | None:
        try:
            res = self._call(self.sdk.markets.bbo, slug)
        except NotFoundError:
            return None
        return res.get("marketData") if isinstance(res, dict) else None

    def get_settlement(self, slug: str) -> Decimal | None:
        self.limiter.wait()
        r = self.http.get(f"{GATEWAY}/v1/markets/{slug}/settlement")
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict):
            return None
        if "settlement" in data:
            return D(data.get("settlement"))
        if "settlementPrice" in data:
            return D(data.get("settlementPrice"))
        return None

    def price_history(self, slug: str, interval: str = "INTERVAL_1W", fidelity: int = 60) -> list[dict]:
        self.limiter.wait()
        r = self.http.get(f"{GATEWAY}/v1/price-history", params={"symbol": slug, "fixedInterval": interval, "fidelity": fidelity})
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("history") or [] if isinstance(data, dict) else []

    def server_clock_skew_seconds(self) -> float | None:
        try:
            r = self.http.head(f"{GATEWAY}/v1/markets", params={"limit": 1})
            date = r.headers.get("date")
            if not date:
                r = self.http.get(f"{GATEWAY}/v1/markets", params={"limit": 1})
                date = r.headers.get("date")
            if not date:
                return None
            server = parsedate_to_datetime(date)
            return (now_utc() - server).total_seconds()
        except Exception:
            return None

    def balances(self) -> dict:
        res = self._call(self.sdk.account.balances)
        bals = res.get("balances") or []
        return bals[0] if bals else {}

    def positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            res = self._call(self.sdk.portfolio.positions, params)
            out.update(res.get("positions") or {})
            cursor = res.get("nextCursor")
            if res.get("eof", True) or not cursor:
                break
        return out

    def open_orders(self, slugs: list[str] | None = None) -> list[dict]:
        params = {"slugs": slugs} if slugs else None
        res = self._call(self.sdk.orders.list, params)
        return res.get("orders") or []

    def get_order(self, order_id: str) -> dict | None:
        try:
            res = self._call(self.sdk.orders.retrieve, order_id)
        except NotFoundError:
            return None
        return res.get("order") if isinstance(res, dict) else None

    def activities(self, cursor: str | None = None, types: list[str] | None = None, limit: int = 100, ascending: bool = True) -> tuple[list[dict], str | None, bool]:
        params: dict[str, Any] = {"limit": limit, "sortOrder": "SORT_ORDER_ASCENDING" if ascending else "SORT_ORDER_DESCENDING"}
        if cursor:
            params["cursor"] = cursor
        if types:
            params["types"] = types
        res = self._call(self.sdk.portfolio.activities, params)
        return res.get("activities") or [], res.get("nextCursor"), bool(res.get("eof", True))

    def preview_order(self, params: dict) -> dict:
        res = self._call(self.sdk.orders.preview, {"request": params})
        return res.get("order") if isinstance(res, dict) else res

    def create_order(self, params: dict) -> dict:
        return self._call(self.sdk.orders.create, params)

    def cancel_order(self, order_id: str, slug: str) -> None:
        self._call(self.sdk.orders.cancel, order_id, {"marketSlug": slug})

    def cancel_all(self, slugs: list[str] | None = None) -> list[str]:
        res = self._call(self.sdk.orders.cancel_all, {"slugs": slugs} if slugs else None)
        return (res or {}).get("canceledOrderIds") or []

    def close_position(self, slug: str) -> dict:
        return self._call(self.sdk.orders.close_position, {"marketSlug": slug, "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"})


def parse_levels(levels: list[dict] | None, drop_placeholders: bool = True) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    for lv in levels or []:
        px = D(lv.get("px"))
        qty = D(lv.get("qty"))
        if px is None or qty is None:
            continue
        if drop_placeholders and (px <= PLACEHOLDER_LOW or px >= PLACEHOLDER_HIGH):
            continue
        out.append((px, qty))
    return out


def book_summary(book: dict | None) -> dict[str, Any]:
    bids = parse_levels((book or {}).get("bids"))
    asks = parse_levels((book or {}).get("offers"))
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return {
        "yes_bid": bids[0][0] if bids else None,
        "yes_ask": asks[0][0] if asks else None,
        "yes_bid_size": bids[0][1] if bids else None,
        "yes_ask_size": asks[0][1] if asks else None,
        "bids": bids[:5],
        "asks": asks[:5],
        "state": (book or {}).get("state"),
        "stats": (book or {}).get("stats") or {},
    }


def market_quotes(market: dict) -> tuple[Decimal | None, Decimal | None]:
    return D(market.get("bestBidQuote")), D(market.get("bestAskQuote"))
