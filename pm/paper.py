from __future__ import annotations

import sqlite3
import uuid
from decimal import ROUND_DOWN, Decimal

from pm.client import PMClient, book_summary
from pm.config import Config, Settings
from pm.db import all_rows, insert, log_event, one, scalar, update
from pm.models import BrokerOrder, OrderRequest
from pm.risk import fee_per_contract, maker_rebate
from pm.util import ONE, ZERO, D, dumps, dstr, iso, loads, now_utc, parse_iso


class PaperBroker:
    mode = "paper"

    def __init__(self, settings: Settings, cfg: Config, conn: sqlite3.Connection, client: PMClient):
        if settings.is_live:
            raise RuntimeError("PaperBroker constructed in live mode")
        self.cfg = cfg
        self.conn = conn
        self.client = client

    def _side_book(self, slug: str, side: str) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]], dict]:
        book = self.client.get_book(slug)
        s = book_summary(book)
        bids, asks = s["bids"], s["asks"]
        if side == "NO":
            nb = sorted([(ONE - px, q) for px, q in asks], key=lambda x: x[0], reverse=True)
            na = sorted([(ONE - px, q) for px, q in bids], key=lambda x: x[0])
            return nb, na, s
        return bids, asks, s

    def preview(self, req: OrderRequest) -> dict:
        bids, asks, _ = self._side_book(req.slug, req.side)
        return {"marketSlug": req.slug, "price": {"value": dstr(req.price), "currency": "USD"}, "quantity": req.qty, "best_bid": dstr(bids[0][0]) if bids else None, "best_ask": dstr(asks[0][0]) if asks else None, "paper": True}

    def create(self, req: OrderRequest) -> BrokerOrder:
        oid = f"paper-{uuid.uuid4().hex[:12]}"
        row = one(self.conn, "SELECT order_id FROM orders WHERE order_id = ?", (req.client_ref,))
        if row is None:
            raise RuntimeError("paper create requires the write-ahead orders row")
        bids, asks, _ = self._side_book(req.slug, req.side)
        out = BrokerOrder(exchange_order_id=oid, status="open", filled_qty=0, avg_price=None, fees=ZERO, raw={"paper": True, "cross_polls": 0})
        is_buy = req.intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT")
        if not is_buy:
            return self._paper_sell(req, oid, bids, out)
        if req.order_type == "market":
            limit = (asks[0][0] + self.cfg.paper.max_slippage) if asks else None
            if limit is None:
                out.status, out.reject_reason = "rejected", "no_asks"
                return out
            self._walk_asks(req, oid, asks, limit, out, taker=True)
            if out.filled_qty < req.qty:
                out.status = "filled" if out.filled_qty > 0 else "rejected"
                if out.filled_qty == 0:
                    out.reject_reason = "insufficient_liquidity"
            return out
        if req.price is None:
            out.status, out.reject_reason = "rejected", "limit_without_price"
            return out
        if asks and asks[0][0] <= req.price:
            if req.post_only:
                out.status, out.reject_reason = "rejected", "post_only_would_cross"
                return out
            self._walk_asks(req, oid, asks, req.price, out, taker=True)
            if out.filled_qty >= req.qty:
                out.status = "filled"
            elif req.tif in ("IOC", "FOK"):
                out.status = "filled" if out.filled_qty > 0 and req.tif == "IOC" else "cancelled"
            else:
                out.status = "partially_filled" if out.filled_qty > 0 else "open"
            return out
        if req.tif in ("IOC", "FOK"):
            out.status, out.reject_reason = "cancelled", "no_immediate_fill"
        return out

    def _walk_asks(self, req: OrderRequest, oid: str, asks, limit: Decimal, out: BrokerOrder, taker: bool) -> None:
        remaining = req.qty
        for px, avail in asks:
            if px > limit or remaining <= 0:
                break
            take = min(remaining, int(avail.to_integral_value(rounding=ROUND_DOWN)))
            if take <= 0:
                continue
            fee = fee_per_contract(px, req.fee_coef) * take if taker else -maker_rebate(px, take)
            self._record_fill(req, oid, px, take, fee, "taker" if taker else "maker", out)
            remaining -= take
        out.exchange_order_id = oid

    def _record_fill(self, req: OrderRequest, oid: str, px: Decimal, qty: int, fee: Decimal, liquidity: str, out: BrokerOrder) -> None:
        ts = iso(now_utc())
        fid = f"{oid}:{uuid.uuid4().hex[:6]}"
        insert(self.conn, "fills", {"order_id": req.client_ref, "exchange_fill_id": fid, "slug": req.slug, "ts": ts, "price": dstr(px), "qty": qty, "fee": dstr(fee, 6), "liquidity": liquidity, "source": "paper_sim"})
        cost = px * qty
        insert(self.conn, "paper_ledger", {"ts": ts, "type": "buy" if req.intent.startswith("ORDER_INTENT_BUY") else "sell", "amount": dstr(-cost if req.intent.startswith("ORDER_INTENT_BUY") else cost, 6), "ref_table": "fills", "ref_id": fid, "note": f"{req.slug} {req.side} {qty}@{dstr(px)}"})
        if fee != 0:
            insert(self.conn, "paper_ledger", {"ts": ts, "type": "fee" if fee > 0 else "rebate", "amount": dstr(-fee, 6), "ref_table": "fills", "ref_id": fid, "note": liquidity})
        self._apply_position(req, px, qty)
        prev_qty = out.filled_qty
        prev_avg = out.avg_price or ZERO
        out.filled_qty = prev_qty + qty
        out.avg_price = ((prev_avg * prev_qty) + px * qty) / out.filled_qty
        out.fees += fee
        out.fills.append({"id": fid, "price": px, "qty": qty, "fee": fee, "ts": ts, "liquidity": liquidity})

    def _apply_position(self, req: OrderRequest, px: Decimal, qty: int) -> None:
        ts = iso(now_utc())
        row = one(self.conn, "SELECT * FROM positions WHERE slug = ? AND side = ?", (req.slug, req.side))
        is_buy = req.intent.startswith("ORDER_INTENT_BUY")
        if is_buy:
            if row is None or row["status"] != "open":
                self.conn.execute("INSERT OR REPLACE INTO positions(slug, side, qty, avg_cost, cost_basis, mark_price, unrealized_pnl, realized_pnl, status, exchange_qty, discrepancy, entry_decision_id, opened_at, closed_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 0, ?, ?, NULL, ?)",
                                  (req.slug, req.side, qty, dstr(px), dstr(px * qty, 6), dstr(px), "0", "0", qty, self._entry_decision(req.client_ref), ts, ts))
            else:
                nq = int(row["qty"]) + qty
                cb = (D(row["cost_basis"], ZERO) or ZERO) + px * qty
                update(self.conn, "positions", {"slug": req.slug, "side": req.side}, {"qty": nq, "cost_basis": dstr(cb, 6), "avg_cost": dstr(cb / nq), "exchange_qty": nq, "updated_at": ts})
        else:
            if row is None:
                return
            nq = max(0, int(row["qty"]) - qty)
            avg = D(row["avg_cost"], ZERO) or ZERO
            realized = (D(row["realized_pnl"], ZERO) or ZERO) + (px - avg) * qty
            update(self.conn, "positions", {"slug": req.slug, "side": req.side}, {"qty": nq, "cost_basis": dstr(avg * nq, 6), "realized_pnl": dstr(realized, 6), "exchange_qty": nq, "status": "open" if nq > 0 else "closed", "closed_at": None if nq > 0 else ts, "updated_at": ts})

    def _entry_decision(self, order_id: str):
        return scalar(self.conn, "SELECT decision_id FROM orders WHERE order_id = ?", (order_id,))

    def _paper_sell(self, req: OrderRequest, oid: str, bids, out: BrokerOrder) -> BrokerOrder:
        pos = one(self.conn, "SELECT qty FROM positions WHERE slug = ? AND side = ? AND status = 'open'", (req.slug, req.side))
        if pos is None or int(pos["qty"]) < req.qty:
            out.status, out.reject_reason = "rejected", "insufficient_position"
            return out
        limit = req.price if req.order_type == "limit" and req.price is not None else ((bids[0][0] - self.cfg.paper.max_slippage) if bids else None)
        if limit is None:
            out.status, out.reject_reason = "rejected", "no_bids"
            return out
        remaining = req.qty
        for px, avail in bids:
            if px < limit or remaining <= 0:
                break
            take = min(remaining, int(avail.to_integral_value(rounding=ROUND_DOWN)))
            if take <= 0:
                continue
            self._record_fill(req, oid, px, take, fee_per_contract(px, req.fee_coef) * take, "taker", out)
            remaining -= take
        out.status = "filled" if remaining == 0 else ("partially_filled" if out.filled_qty > 0 else ("open" if req.order_type == "limit" else "rejected"))
        return out

    def cancel(self, exchange_order_id: str, slug: str) -> None:
        return None

    def cancel_all(self) -> list[str]:
        rows = all_rows(self.conn, "SELECT exchange_order_id FROM orders WHERE status IN ('open','partially_filled','submitted')")
        return [r["exchange_order_id"] for r in rows if r["exchange_order_id"]]

    def get_order(self, exchange_order_id: str) -> BrokerOrder | None:
        r = one(self.conn, "SELECT * FROM orders WHERE exchange_order_id = ?", (exchange_order_id,))
        if r is None:
            return None
        return BrokerOrder(exchange_order_id=exchange_order_id, status=r["status"], filled_qty=int(r["filled_qty"] or 0), avg_price=D(r["avg_fill_price"]), fees=D(r["fees_paid"], ZERO) or ZERO, raw=loads(r["raw_response_json"], {}) or {})

    def open_orders(self) -> list[BrokerOrder]:
        rows = all_rows(self.conn, "SELECT exchange_order_id FROM orders WHERE status IN ('open','partially_filled') AND exchange_order_id IS NOT NULL")
        return [o for o in (self.get_order(r["exchange_order_id"]) for r in rows) if o]

    def positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in all_rows(self.conn, "SELECT * FROM positions WHERE status = 'open' AND qty > 0"):
            out[r["slug"]] = {"side": r["side"], "qty": int(r["qty"]), "cost_basis": D(r["cost_basis"], ZERO), "avg_cost": D(r["avg_cost"]), "realized": D(r["realized_pnl"], ZERO), "expired": False, "raw": dict(r)}
        return out

    def cash(self) -> Decimal:
        v = scalar(self.conn, "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM paper_ledger", default=0)
        return Decimal(str(round(float(v), 6)))

    def balances(self) -> dict:
        cash = self.cash()
        reserved = ZERO
        for r in all_rows(self.conn, "SELECT limit_price, qty, filled_qty FROM orders WHERE status IN ('open','partially_filled') AND intent IN ('ORDER_INTENT_BUY_LONG','ORDER_INTENT_BUY_SHORT')"):
            reserved += (D(r["limit_price"], ZERO) or ZERO) * Decimal(int(r["qty"]) - int(r["filled_qty"] or 0))
        mark = ZERO
        for r in all_rows(self.conn, "SELECT qty, mark_price, cost_basis FROM positions WHERE status = 'open' AND qty > 0"):
            mp = D(r["mark_price"])
            mark += (mp * Decimal(r["qty"])) if mp is not None else (D(r["cost_basis"], ZERO) or ZERO)
        return {"cash": cash, "buying_power": cash - reserved, "positions_mark": mark, "open_orders_notional": reserved, "raw": {"paper": True}}

    def close_position(self, slug: str) -> dict:
        raise NotImplementedError("use execution.exit_position for paper mode")

    def poll(self, now=None) -> list[dict]:
        now = now or now_utc()
        events: list[dict] = []
        rows = all_rows(self.conn, "SELECT * FROM orders WHERE status IN ('open','partially_filled') AND order_type = 'limit'")
        for r in rows:
            exp = parse_iso(r["expires_at"])
            if exp is not None and now >= exp:
                update(self.conn, "orders", {"order_id": r["order_id"]}, {"status": "expired", "closed_at": iso(now), "updated_at": iso(now), "cancel_reason": "gtd_expired"})
                events.append({"type": "expired", "order_id": r["order_id"], "slug": r["slug"]})
                continue
            price = D(r["limit_price"])
            if price is None:
                continue
            side = r["side"]
            is_buy = r["intent"].startswith("ORDER_INTENT_BUY")
            bids, asks, summary = self._side_book(r["slug"], side)
            raw = loads(r["raw_response_json"], {}) or {}
            crossed = False
            avail = ZERO
            if is_buy and asks and asks[0][0] <= price:
                crossed = True
                avail = sum((q for px, q in asks if px <= price), ZERO)
            if (not is_buy) and bids and bids[0][0] >= price:
                crossed = True
                avail = sum((q for px, q in bids if px >= price), ZERO)
            polls = int(raw.get("cross_polls", 0)) + 1 if crossed else 0
            raw["cross_polls"] = polls
            raw["last_poll"] = iso(now)
            remaining = int(r["qty"]) - int(r["filled_qty"] or 0)
            if crossed and polls >= 2 and remaining > 0:
                fill_qty = min(remaining, int((avail * self.cfg.paper.maker_fill_fraction).to_integral_value(rounding=ROUND_DOWN)))
                if fill_qty > 0:
                    req = OrderRequest(slug=r["slug"], side=side, intent=r["intent"], order_type="limit", tif=r["tif"], qty=int(r["qty"]), price=price, post_only=bool(r["post_only"]), good_till=exp, client_ref=r["order_id"], fee_coef=D(self._fee_coef(r["slug"]), Decimal("0.06")))
                    out = BrokerOrder(exchange_order_id=r["exchange_order_id"], status=r["status"], filled_qty=int(r["filled_qty"] or 0), avg_price=D(r["avg_fill_price"]), fees=D(r["fees_paid"], ZERO) or ZERO, raw=raw)
                    self._record_fill(req, r["exchange_order_id"], price, fill_qty, -maker_rebate(price, fill_qty), "maker", out)
                    new_status = "filled" if out.filled_qty >= int(r["qty"]) else "partially_filled"
                    update(self.conn, "orders", {"order_id": r["order_id"]}, {"filled_qty": out.filled_qty, "avg_fill_price": dstr(out.avg_price), "fees_paid": dstr(out.fees, 6), "status": new_status, "closed_at": iso(now) if new_status == "filled" else None, "raw_response_json": dumps(raw), "updated_at": iso(now)})
                    events.append({"type": "fill", "order_id": r["order_id"], "slug": r["slug"], "side": side, "qty": fill_qty, "price": price, "status": new_status})
                    log_event(self.conn, "paper_engine", "info", "paper_fill", "orders", r["order_id"], {"qty": fill_qty, "price": dstr(price)})
                    continue
            update(self.conn, "orders", {"order_id": r["order_id"]}, {"raw_response_json": dumps(raw), "updated_at": iso(now)})
        return events

    def _fee_coef(self, slug: str):
        return scalar(self.conn, "SELECT fee_coefficient FROM markets WHERE slug = ?", (slug,), "0.06")
