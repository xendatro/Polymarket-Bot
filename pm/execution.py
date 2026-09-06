from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from decimal import Decimal

from filelock import FileLock, Timeout

from pm.broker import BrokerTimeout
from pm.config import Config, Settings
from pm.db import all_rows, get_control, insert, log_event, one, update
from pm.models import EXIT_INTENT_BY_SIDE, LIVE_STATUSES, TERMINAL_STATUSES, BrokerOrder, Decision, MarketInfo, OrderRequest
from pm.risk import control_state
from pm.util import ZERO, D, dstr, dumps, iso, now_utc, parse_iso, sha256_short


class PlaceResult:
    def __init__(self, order_id: str | None, status: str, reason: str = "", broker: BrokerOrder | None = None, duplicate: bool = False):
        self.order_id = order_id
        self.status = status
        self.reason = reason
        self.broker = broker
        self.duplicate = duplicate

    def as_dict(self) -> dict:
        return {"order_id": self.order_id, "status": self.status, "reason": self.reason, "duplicate": self.duplicate, "filled_qty": self.broker.filled_qty if self.broker else 0, "avg_price": dstr(self.broker.avg_price) if self.broker and self.broker.avg_price else None}


def idempotency_key(mode: str, scope: str, slug: str, intent: str, price: Decimal | None, qty: int) -> str:
    return sha256_short(f"{mode}|{scope}|{slug}|{intent}|{dstr(price) if price is not None else 'mkt'}|{qty}")


def orders_lock(settings: Settings) -> FileLock:
    return FileLock(str(settings.locks_dir / "orders.lock"), timeout=30)


def place_order(settings: Settings, cfg: Config, conn: sqlite3.Connection, broker, decision: Decision, market: MarketInfo, run_id: str | None, source: str, scope: str | None = None) -> PlaceResult:
    if not decision.go or decision.proposed_qty <= 0 or decision.side is None or decision.intent is None:
        return PlaceResult(None, "rejected", "decision_not_go")
    if settings.dry_run:
        return PlaceResult(None, "dry_run", "PM_DRY_RUN=1")
    now = now_utc()
    scope = scope or run_id or f"adhoc-{now.strftime('%Y%m%d%H%M%S')}"
    key = idempotency_key(settings.mode, scope, decision.slug, decision.intent, decision.proposed_price, decision.proposed_qty)
    try:
        lock = orders_lock(settings)
        lock.acquire()
    except Timeout:
        return PlaceResult(None, "rejected", "orders_lock_busy")
    try:
        cs = control_state(conn, cfg, now)
        if not cs.trading_enabled:
            return PlaceResult(None, "rejected", f"trading_disabled:{cs.kill_reason}")
        if cs.paused_until is not None and cs.paused_until > now:
            return PlaceResult(None, "rejected", f"paused:{cs.pause_reason}")
        if cs.in_maintenance:
            return PlaceResult(None, "rejected", "maintenance_window")
        if settings.is_live and get_control(conn, "trading_enabled", "0") != "1":
            return PlaceResult(None, "rejected", "live_not_armed")
        existing = one(conn, "SELECT order_id, status FROM orders WHERE idempotency_key = ?", (key,))
        if existing is not None:
            return PlaceResult(existing["order_id"], existing["status"], "duplicate_idempotency_key", duplicate=True)
        if run_id:
            dup = one(conn, "SELECT order_id, status FROM orders WHERE run_id = ? AND slug = ? AND intent = ?", (run_id, decision.slug, decision.intent))
            if dup is not None:
                return PlaceResult(dup["order_id"], dup["status"], "duplicate_run_slug_intent", duplicate=True)
        pending = one(conn, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending_submit','unknown')")
        if pending and int(pending["n"]) > 0:
            return PlaceResult(None, "rejected", "unresolved_pending_orders")
        order_id = uuid.uuid4().hex
        ts = iso(now)
        row = {
            "order_id": order_id,
            "idempotency_key": key,
            "exchange_order_id": None,
            "decision_id": decision.decision_id,
            "run_id": run_id,
            "slug": decision.slug,
            "mode": settings.mode,
            "source": source,
            "intent": decision.intent,
            "side": decision.side,
            "order_type": decision.order_type,
            "tif": decision.tif,
            "post_only": 1 if decision.post_only else 0,
            "limit_price": dstr(decision.proposed_price),
            "qty": decision.proposed_qty,
            "filled_qty": 0,
            "status": "pending_submit",
            "expires_at": iso(decision.expires_at),
            "created_at": ts,
            "updated_at": ts,
        }
        insert(conn, "orders", row)
        req = OrderRequest(slug=decision.slug, side=decision.side, intent=decision.intent, order_type=decision.order_type, tif=decision.tif, qty=decision.proposed_qty, price=decision.proposed_price, post_only=decision.post_only, good_till=decision.expires_at, client_ref=order_id, tick=market.tick, fee_coef=market.fee_coef)
        try:
            preview = broker.preview(req)
        except Exception as e:
            update(conn, "orders", {"order_id": order_id}, {"status": "rejected", "error_text": f"preview_failed:{e}", "closed_at": ts, "updated_at": ts})
            log_event(conn, "execution", "warn", "preview_failed", "orders", order_id, {"error": str(e)})
            return PlaceResult(order_id, "rejected", f"preview_failed:{e}")
        drift = _preview_drift(preview, req)
        update(conn, "orders", {"order_id": order_id}, {"preview_json": dumps(preview), "updated_at": ts})
        if drift:
            update(conn, "orders", {"order_id": order_id}, {"status": "rejected", "error_text": f"preview_drift:{drift}", "closed_at": ts, "updated_at": ts})
            log_event(conn, "execution", "warn", "preview_drift", "orders", order_id, {"drift": drift, "preview": preview})
            return PlaceResult(order_id, "rejected", f"preview_drift:{drift}")
        try:
            result = broker.create(req)
        except BrokerTimeout as e:
            update(conn, "orders", {"order_id": order_id}, {"status": "unknown", "submitted_at": ts, "error_text": f"timeout:{e}", "updated_at": iso(now_utc())})
            log_event(conn, "execution", "error", "order_unknown_after_timeout", "orders", order_id, {"error": str(e)})
            return PlaceResult(order_id, "unknown", "timeout_after_submit")
        except Exception as e:
            update(conn, "orders", {"order_id": order_id}, {"status": "rejected", "submitted_at": ts, "error_text": f"create_failed:{e}", "closed_at": iso(now_utc()), "updated_at": iso(now_utc())})
            log_event(conn, "execution", "error", "order_create_failed", "orders", order_id, {"error": str(e)})
            return PlaceResult(order_id, "rejected", f"create_failed:{e}")
        _apply_broker_order(conn, order_id, result, submitted_at=ts)
        log_event(conn, "execution", "info", "order_placed", "orders", order_id, {"slug": decision.slug, "side": decision.side, "qty": decision.proposed_qty, "price": dstr(decision.proposed_price), "status": result.status, "mode": settings.mode})
        return PlaceResult(order_id, result.status, result.reject_reason, broker=result)
    finally:
        lock.release()


def _preview_drift(preview: dict, req: OrderRequest) -> str:
    if not isinstance(preview, dict):
        return ""
    px = D(preview.get("price"))
    if req.order_type == "limit" and req.price is not None and px is not None and abs(px - req.price) > req.tick:
        return f"price {px} vs {req.price}"
    q = preview.get("quantity")
    if q is not None and int(D(q, Decimal(req.qty))) != req.qty:
        return f"qty {q} vs {req.qty}"
    return ""


def _apply_broker_order(conn: sqlite3.Connection, order_id: str, bo: BrokerOrder, submitted_at: str | None = None) -> None:
    ts = iso(now_utc())
    vals = {
        "exchange_order_id": bo.exchange_order_id,
        "status": bo.status,
        "filled_qty": bo.filled_qty,
        "avg_fill_price": dstr(bo.avg_price),
        "fees_paid": dstr(bo.fees, 6),
        "raw_response_json": dumps(bo.raw),
        "updated_at": ts,
    }
    if submitted_at:
        vals["submitted_at"] = submitted_at
    if bo.status in TERMINAL_STATUSES:
        vals["closed_at"] = ts
    if bo.reject_reason:
        vals["error_text"] = bo.reject_reason
    update(conn, "orders", {"order_id": order_id}, vals)
    for f in bo.fills:
        fid = f.get("id")
        if fid and one(conn, "SELECT fill_id FROM fills WHERE exchange_fill_id = ?", (fid,)):
            continue
        row = one(conn, "SELECT slug FROM orders WHERE order_id = ?", (order_id,))
        if f.get("source") == "paper_sim" or (fid and str(fid).startswith("paper-")):
            continue
        insert(conn, "fills", {"order_id": order_id, "exchange_fill_id": fid, "slug": row["slug"] if row else "", "ts": f.get("ts") or ts, "price": dstr(D(f.get("price"))), "qty": int(f.get("qty") or 0), "fee": dstr(D(f.get("fee"), ZERO), 6), "liquidity": "taker" if f.get("aggressor") else "maker", "source": "live"})


def cancel_order(conn: sqlite3.Connection, broker, order_id: str, reason: str) -> bool:
    r = one(conn, "SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if r is None or r["status"] in TERMINAL_STATUSES:
        return False
    ts = iso(now_utc())
    if r["exchange_order_id"] and broker.mode == "live":
        try:
            broker.cancel(r["exchange_order_id"], r["slug"])
            update(conn, "orders", {"order_id": order_id}, {"status": "cancel_requested", "cancel_reason": reason, "updated_at": ts})
        except Exception as e:
            log_event(conn, "execution", "warn", "cancel_failed", "orders", order_id, {"error": str(e)})
            return False
    else:
        update(conn, "orders", {"order_id": order_id}, {"status": "cancelled", "cancel_reason": reason, "closed_at": ts, "updated_at": ts})
    log_event(conn, "execution", "info", "order_cancel", "orders", order_id, {"reason": reason})
    return True


def expire_orders(conn: sqlite3.Connection, broker, now=None) -> int:
    now = now or now_utc()
    n = 0
    for r in all_rows(conn, "SELECT order_id, expires_at FROM orders WHERE status IN ('open','partially_filled','submitted') AND expires_at IS NOT NULL"):
        exp = parse_iso(r["expires_at"])
        if exp is not None and now >= exp:
            if cancel_order(conn, broker, r["order_id"], "expired_by_runner"):
                n += 1
    return n


def reconcile(settings: Settings, cfg: Config, conn: sqlite3.Connection, broker, now=None) -> dict:
    now = now or now_utc()
    report = {"updated": 0, "orphans": 0, "unresolved": 0, "positions": 0, "discrepancies": 0, "errors": []}
    if broker.mode != "live":
        for ev in broker.poll(now):
            report.setdefault("paper_events", []).append(ev)
        report["unresolved"] = int(one(conn, "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending_submit','unknown')")["n"])
        _mark_positions(conn, broker)
        return report
    try:
        exchange_open = {o.exchange_order_id: o for o in broker.open_orders() if o.exchange_order_id}
    except Exception as e:
        report["errors"].append(f"open_orders:{e}")
        exchange_open = None
    for r in all_rows(conn, "SELECT * FROM orders WHERE status IN ('pending_submit','submitted','open','partially_filled','cancel_requested','unknown')"):
        if r["exchange_order_id"]:
            try:
                bo = broker.get_order(r["exchange_order_id"])
            except Exception as e:
                report["errors"].append(f"get_order:{e}")
                continue
            if bo is not None:
                _apply_broker_order(conn, r["order_id"], bo)
                report["updated"] += 1
            continue
        age = now - (parse_iso(r["created_at"]) or now)
        if age < timedelta(seconds=60):
            report["unresolved"] += 1
            continue
        matched = None
        if exchange_open:
            for eo in exchange_open.values():
                raw = eo.raw
                if raw.get("marketSlug") == r["slug"] and raw.get("intent") == r["intent"] and int(D(raw.get("quantity"), ZERO) or 0) == int(r["qty"]) and dstr(D(raw.get("price"))) == dstr(D(r["limit_price"])):
                    matched = eo
                    break
        if matched is not None:
            _apply_broker_order(conn, r["order_id"], matched)
            log_event(conn, "reconcile", "info", "pending_matched", "orders", r["order_id"], {"exchange_order_id": matched.exchange_order_id})
            report["updated"] += 1
        else:
            attempts = int(D((r["error_text"] or "").split("attempts=")[-1].split(";")[0], ZERO) or 0) if "attempts=" in (r["error_text"] or "") else 0
            attempts += 1
            if attempts >= 2:
                update(conn, "orders", {"order_id": r["order_id"]}, {"status": "rejected", "error_text": f"assumed_never_sent;attempts={attempts}", "closed_at": iso(now), "updated_at": iso(now)})
                log_event(conn, "reconcile", "warn", "assumed_never_sent", "orders", r["order_id"], None)
            else:
                update(conn, "orders", {"order_id": r["order_id"]}, {"error_text": f"unmatched;attempts={attempts}", "updated_at": iso(now)})
                report["unresolved"] += 1
    if exchange_open is not None:
        known = {r["exchange_order_id"] for r in all_rows(conn, "SELECT exchange_order_id FROM orders WHERE exchange_order_id IS NOT NULL")}
        for eid, eo in exchange_open.items():
            if eid in known:
                continue
            raw = eo.raw
            ts = iso(now)
            oid = uuid.uuid4().hex
            insert(conn, "orders", {"order_id": oid, "idempotency_key": f"orphan-{eid}", "exchange_order_id": eid, "run_id": None, "slug": raw.get("marketSlug") or "", "mode": settings.mode, "source": "orphan", "intent": raw.get("intent") or "", "side": "YES" if "LONG" in (raw.get("intent") or "") else "NO", "order_type": "limit" if raw.get("type") == "ORDER_TYPE_LIMIT" else "market", "tif": "GTC", "limit_price": dstr(D(raw.get("price"))), "qty": int(D(raw.get("quantity"), ZERO) or 0), "filled_qty": eo.filled_qty, "status": "orphan_adopted", "raw_response_json": dumps(raw), "created_at": ts, "updated_at": ts})
            try:
                broker.cancel(eid, raw.get("marketSlug") or "")
            except Exception as e:
                report["errors"].append(f"orphan_cancel:{e}")
            log_event(conn, "reconcile", "warn", "orphan_found", "orders", oid, {"exchange_order_id": eid})
            report["orphans"] += 1
    try:
        expos = broker.positions()
    except Exception as e:
        report["errors"].append(f"positions:{e}")
        expos = None
    if expos is not None:
        ts = iso(now)
        seen = set()
        for slug, p in expos.items():
            seen.add((slug, p["side"]))
            row = one(conn, "SELECT * FROM positions WHERE slug = ? AND side = ?", (slug, p["side"]))
            disc = 1 if (row is not None and int(row["qty"]) != p["qty"] and row["status"] == "open") else 0
            if disc:
                report["discrepancies"] += 1
                log_event(conn, "reconcile", "warn", "position_discrepancy", "positions", slug, {"db_qty": int(row["qty"]), "exchange_qty": p["qty"]})
            conn.execute(
                "INSERT INTO positions(slug, side, qty, avg_cost, cost_basis, mark_price, unrealized_pnl, realized_pnl, status, exchange_qty, discrepancy, entry_decision_id, opened_at, closed_at, updated_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'open', ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(slug, side) DO UPDATE SET qty = excluded.qty, avg_cost = excluded.avg_cost, cost_basis = excluded.cost_basis, realized_pnl = excluded.realized_pnl, status = 'open', exchange_qty = excluded.exchange_qty, discrepancy = excluded.discrepancy, closed_at = NULL, updated_at = excluded.updated_at",
                (slug, p["side"], p["qty"], dstr(p.get("avg_cost")), dstr(p.get("cost_basis"), 6), dstr(p.get("realized"), 6), p["qty"], disc, row["entry_decision_id"] if row else None, row["opened_at"] if row else ts, ts),
            )
            report["positions"] += 1
        for r in all_rows(conn, "SELECT slug, side FROM positions WHERE status = 'open'"):
            if (r["slug"], r["side"]) not in seen:
                update(conn, "positions", {"slug": r["slug"], "side": r["side"]}, {"qty": 0, "exchange_qty": 0, "status": "closed", "closed_at": ts, "updated_at": ts})
    _mark_positions(conn, broker)
    return report


def _mark_positions(conn: sqlite3.Connection, broker) -> None:
    ts = iso(now_utc())
    for r in all_rows(conn, "SELECT slug, side, qty, cost_basis FROM positions WHERE status = 'open' AND qty > 0"):
        snap = one(conn, "SELECT yes_bid, yes_ask FROM market_snapshots WHERE slug = ? ORDER BY snapshot_id DESC LIMIT 1", (r["slug"],))
        if snap is None:
            continue
        yb, ya = D(snap["yes_bid"]), D(snap["yes_ask"])
        mark = yb if r["side"] == "YES" else ((Decimal(1) - ya) if ya is not None else None)
        if mark is None:
            continue
        cb = D(r["cost_basis"], ZERO) or ZERO
        update(conn, "positions", {"slug": r["slug"], "side": r["side"]}, {"mark_price": dstr(mark), "unrealized_pnl": dstr(mark * Decimal(r["qty"]) - cb, 6), "updated_at": ts})


def snapshot_balance(conn: sqlite3.Connection, broker, source: str) -> dict:
    b = broker.balances()
    cash = b.get("cash") or ZERO
    reserved = b.get("open_orders_notional") or ZERO
    mark = b.get("positions_mark") or ZERO
    equity = cash + mark
    insert(conn, "balance_snapshots", {"ts": iso(now_utc()), "cash": dstr(cash, 4), "reserved": dstr(reserved, 4), "positions_mark": dstr(mark, 4), "equity": dstr(equity, 4), "buying_power": dstr(b.get("buying_power"), 4), "source": source})
    return {"cash": cash, "reserved": reserved, "positions_mark": mark, "equity": equity, "buying_power": b.get("buying_power")}


def exit_position(settings: Settings, cfg: Config, conn: sqlite3.Connection, broker, slug: str, side: str, market: MarketInfo, quote, run_id: str | None, source: str, reason: str) -> PlaceResult:
    pos = one(conn, "SELECT * FROM positions WHERE slug = ? AND side = ? AND status = 'open' AND qty > 0", (slug, side))
    if pos is None:
        return PlaceResult(None, "rejected", "no_open_position")
    bid, ask = quote.side_prices(side)
    if bid is None:
        return PlaceResult(None, "rejected", "no_bid")
    d = Decision(slug=slug, source=source, go=True, nogo_reasons=[], side=side, intent=EXIT_INTENT_BY_SIDE[side], proposed_price=bid, proposed_qty=int(pos["qty"]), order_type="limit", tif="IOC", post_only=False, rationale=reason, tick=market.tick, fee_coef=market.fee_coef)
    d.decision_id = insert(conn, "decisions", d.to_row(run_id, "", iso(now_utc())))
    if settings.dry_run:
        return PlaceResult(None, "dry_run", "PM_DRY_RUN=1")
    return _place_exit(settings, cfg, conn, broker, d, market, run_id, source)


def _place_exit(settings, cfg, conn, broker, d: Decision, market, run_id, source) -> PlaceResult:
    now = now_utc()
    key = idempotency_key(settings.mode, run_id or f"exit-{now.strftime('%Y%m%d%H%M%S')}", d.slug, d.intent, d.proposed_price, d.proposed_qty)
    order_id = uuid.uuid4().hex
    ts = iso(now)
    with orders_lock(settings):
        insert(conn, "orders", {"order_id": order_id, "idempotency_key": key, "decision_id": d.decision_id, "run_id": run_id, "slug": d.slug, "mode": settings.mode, "source": source, "intent": d.intent, "side": d.side, "order_type": d.order_type, "tif": d.tif, "post_only": 0, "limit_price": dstr(d.proposed_price), "qty": d.proposed_qty, "filled_qty": 0, "status": "pending_submit", "created_at": ts, "updated_at": ts})
        req = OrderRequest(slug=d.slug, side=d.side, intent=d.intent, order_type=d.order_type, tif=d.tif, qty=d.proposed_qty, price=d.proposed_price, post_only=False, good_till=None, client_ref=order_id, tick=market.tick, fee_coef=market.fee_coef)
        try:
            result = broker.create(req)
        except BrokerTimeout as e:
            update(conn, "orders", {"order_id": order_id}, {"status": "unknown", "submitted_at": ts, "error_text": f"timeout:{e}", "updated_at": iso(now_utc())})
            return PlaceResult(order_id, "unknown", "timeout_after_submit")
        except Exception as e:
            update(conn, "orders", {"order_id": order_id}, {"status": "rejected", "submitted_at": ts, "error_text": f"create_failed:{e}", "closed_at": iso(now_utc()), "updated_at": iso(now_utc())})
            return PlaceResult(order_id, "rejected", f"create_failed:{e}")
        _apply_broker_order(conn, order_id, result, submitted_at=ts)
        log_event(conn, "execution", "info", "exit_placed", "orders", order_id, {"slug": d.slug, "side": d.side, "qty": d.proposed_qty, "price": dstr(d.proposed_price), "status": result.status})
        return PlaceResult(order_id, result.status, result.reject_reason, broker=result)
