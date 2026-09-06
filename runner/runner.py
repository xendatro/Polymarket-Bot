from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler

import requests

from pm.broker import make_broker
from pm.calibrate import recompute, refresh_observed_settlements
from pm.client import PMClient
from pm.config import REPO_ROOT, Config, Settings, load_config, load_settings
from pm.db import all_rows, connect, get_control, init_schema, insert, log_event, one, set_control, update
from pm.discord_reports import post_hourly_reports
from pm.execution import _apply_broker_order, _mark_positions, expire_orders, place_order, reconcile, snapshot_balance
from pm.manage import exit_levels, manage_positions
from pm.notify import embed, money, post_webhook
from pm.report import compute_daily_stats, render_daily_png
from pm.risk import account_from_db, control_state
from pm.rules import decide_favorite, event_key, scan_favorites
from pm.scan import fetch_quote, market_from_db, record_snapshot
from pm.settle import settle_positions
from pm.util import ZERO, D, dstr, dumps, iso, local_tz, now_utc, parse_iso

log = logging.getLogger("runner")


def setup_logging(settings: Settings) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = TimedRotatingFileHandler(settings.logs_dir / "runner.log", when="midnight", backupCount=14, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Runner:
    def __init__(self, settings: Settings, cfg: Config, cfg_hash: str):
        self.settings = settings
        self.cfg = cfg
        self.cfg_hash = cfg_hash
        self.conn = connect(settings.db_path)
        init_schema(self.conn, settings.mode, cfg.paper.starting_cash)
        self.client = PMClient(settings)
        self.broker = make_broker(settings, cfg, self.conn, self.client)
        self.stop = False
        self._last_settle_check = datetime.min.replace(tzinfo=now_utc().tzinfo)
        self._last_mark = self._last_settle_check

    def post(self, channel: str, **kw) -> bool:
        url = getattr(self.settings, f"webhook_{channel}", None)
        return post_webhook(url, conn=self.conn, channel=channel, **kw)

    def announce_start(self) -> None:
        set_control(self.conn, "runner_started_at", iso(now_utc()))
        armed = get_control(self.conn, "trading_enabled", "0") == "1"
        self.post("trades", embeds=[embed(f"[{self.settings.mode.upper()}] Runner started", f"every {self.cfg.run_interval_hours}h · dry run: {'yes' if self.settings.dry_run else 'no'} · trading: {'on' if armed else 'off'}", "purple")])
        log.info("runner started mode=%s dry_run=%s db=%s", self.settings.mode, self.settings.dry_run, self.settings.db_path)

    def preflight(self, now: datetime) -> dict:
        skew = self.client.server_clock_skew_seconds()
        stop_file = (REPO_ROOT / "STOP").exists()
        if stop_file and get_control(self.conn, "trading_enabled", "0") == "1":
            set_control(self.conn, "trading_enabled", "0")
            set_control(self.conn, "kill_reason", "STOP file present")
            log_event(self.conn, "runner", "warn", "kill_set", None, None, {"reason": "STOP file"})
            self.post("alerts", embeds=[embed("Trading halted", "STOP file found in the repo folder; remove it and /resume to trade again.", "red")])
        cs = control_state(self.conn, self.cfg, now)
        pf = {"clock_skew_s": skew, "stop_file": stop_file, "trading_enabled": cs.trading_enabled, "paused_until": iso(cs.paused_until), "pause_reason": cs.pause_reason, "kill_reason": cs.kill_reason, "in_maintenance": cs.in_maintenance, "recent_losses": cs.recent_losses, "daily_realized": dstr(cs.daily_realized_pnl, 4)}
        if skew is not None and abs(skew) > 5:
            log.warning("clock skew %.1fs", skew)
            pf["warning"] = f"clock skew {skew:.1f}s"
        return pf

    def run_once(self, trigger: str = "schedule") -> str:
        now = now_utc()
        run_id = uuid.uuid4().hex[:12]
        insert(self.conn, "runs", {"run_id": run_id, "started_at": iso(now), "status": "running", "trigger": trigger, "mode": self.settings.mode, "dry_run": 1 if self.settings.dry_run else 0, "config_hash": self.cfg_hash, "git_sha": _git_sha()})
        set_control(self.conn, "last_run_id", run_id)
        summary: dict = {"orders": [], "decisions": [], "sold": [], "warnings": [], "orders_placed": 0, "pool": 0, "scanned": 0}
        status = "failed"
        error_text = None
        log.info("run %s start (%s)", run_id, trigger)
        try:
            pf = self.preflight(now)
            update(self.conn, "runs", {"run_id": run_id}, {"preflight_json": dumps(pf)})
            if pf.get("warning"):
                summary["warnings"].append(pf["warning"])
            rec = reconcile(self.settings, self.cfg, self.conn, self.broker, now)
            for err in rec.get("errors", []):
                summary["warnings"].append(f"reconcile: {err}")
            if rec.get("orphans"):
                summary["warnings"].append(f"{rec['orphans']} unknown order(s) on the exchange were cancelled")
            settle_positions(self.conn, self.client, self.cfg, self.settings.mode, now)
            expire_orders(self.conn, self.broker, now)
            for ev in manage_positions(self.settings, self.cfg, self.conn, self.client, self.broker, now):
                if ev.get("type") in ("take_profit", "stop_loss"):
                    summary["sold"].append({"slug": ev["slug"], "type": ev["type"], "qty": ev.get("qty"), "price": dstr(ev.get("price")), "status": ev.get("status")})
            snapshot_balance(self.conn, self.broker, "run_start")
            status = self._buy_favorites(run_id, now, summary)
            return run_id
        except Exception as e:
            status = "failed"
            error_text = traceback.format_exc()[-3000:]
            summary["warnings"].append(f"{type(e).__name__}: {e}")
            log.exception("run %s failed", run_id)
            return run_id
        finally:
            try:
                snapshot_balance(self.conn, self.broker, "run_end")
                acct = account_from_db(self.conn, self.settings.mode, now_utc())
                summary["equity"], summary["cash"], summary["invested"], summary["invested_pct"] = acct.total, acct.cash, acct.invested, acct.invested_pct
            except Exception as e:
                summary["warnings"].append(f"balance: {e}")
            update(self.conn, "runs", {"run_id": run_id}, {"finished_at": iso(now_utc()), "status": status, "error_text": error_text, "summary_json": dumps(summary)})
            try:
                post_hourly_reports(self.conn, self.cfg, self.settings, now_utc())
            except Exception as e:
                log.warning("report failed: %s", e)
            if status == "failed":
                self.post("alerts", embeds=[embed(f"Run {run_id[:8]} failed", (error_text or "")[-1400:], "red")])
            if self.settings.healthcheck_url and status in ("ok", "no_candidates"):
                try:
                    requests.get(self.settings.healthcheck_url, timeout=10)
                except Exception:
                    pass
            log.info("run %s finished status=%s orders=%s", run_id, status, summary.get("orders_placed"))

    def _buy_favorites(self, run_id: str, now: datetime, summary: dict) -> str:
        scanned, picks = scan_favorites(self.conn, self.client, self.cfg, run_id, now)
        summary["scanned"], summary["pool"] = scanned, len(picks)
        update(self.conn, "runs", {"run_id": run_id}, {"candidates_scanned": scanned})
        cat_counts: dict[str, int] = {}
        event_counts: dict[str, int] = {}
        for r in all_rows(self.conn, "SELECT slug FROM positions WHERE status = 'open' AND qty > 0"):
            mk = market_from_db(self.conn, r["slug"])
            if mk is None:
                continue
            cat_counts[mk.category or "other"] = cat_counts.get(mk.category or "other", 0) + 1
            event_counts[event_key(mk)] = event_counts.get(event_key(mk), 0) + 1
        placed = 0
        n_dec = 0
        for p in picks:
            if placed >= self.cfg.max_new_orders_per_run:
                break
            q = fetch_quote(self.client, p.market.slug)
            if q is None:
                continue
            record_snapshot(self.conn, p.market.slug, q, "scan", now)
            if q.open_interest is None:
                q.open_interest = p.open_interest
            account = account_from_db(self.conn, self.settings.mode, now)
            cs = control_state(self.conn, self.cfg, now)
            d = decide_favorite(self.cfg, p, q, account, cs, now, cat_counts, event_counts)
            d.decision_id = insert(self.conn, "decisions", d.to_row(run_id, self.cfg_hash, iso(now)))
            n_dec += 1
            summary["decisions"].append({"slug": d.slug, "side": d.side, "price": dstr(d.proposed_price), "qty": d.proposed_qty, "go": d.go, "nogo": d.nogo_reasons, "score": dstr(p.score, 4)})
            if not d.go:
                budget_left = self.cfg.portfolio.invest_target_pct * account.total - account.invested
                if "portfolio_target_reached" in d.nogo_reasons or "max_open_positions" in d.nogo_reasons or "trading_disabled" in d.nogo_reasons or budget_left < self.cfg.favorites.price_min:
                    break
                continue
            pr = place_order(self.settings, self.cfg, self.conn, self.broker, d, p.market, run_id, "rules")
            summary["orders"].append({"slug": d.slug, "side": d.side, "qty": d.proposed_qty, "price": dstr(d.proposed_price), "status": pr.status, "reason": pr.reason})
            if pr.order_id and pr.status not in ("rejected", "dry_run") and not pr.duplicate:
                placed += 1
                cat_counts[p.market.category or "other"] = cat_counts.get(p.market.category or "other", 0) + 1
                event_counts[event_key(p.market)] = event_counts.get(event_key(p.market), 0) + 1
            elif pr.status == "dry_run":
                summary["warnings"].append(f"dry run: would buy {d.proposed_qty} {d.side} at {dstr(d.proposed_price)} on {p.market.title or d.slug}")
        update(self.conn, "runs", {"run_id": run_id}, {"decisions": n_dec, "orders_placed": placed})
        summary["orders_placed"] = placed
        return "ok" if picks else "no_candidates"

    def poll_tick(self, now: datetime) -> None:
        set_control(self.conn, "last_heartbeat", iso(now))
        if self.broker.mode == "paper":
            self.broker.poll(now)
        else:
            self._refresh_live_orders()
        expire_orders(self.conn, self.broker, now)
        if now - self._last_settle_check > timedelta(minutes=10):
            self._last_settle_check = now
            try:
                settle_positions(self.conn, self.client, self.cfg, self.settings.mode, now)
            except Exception as e:
                log.warning("settle check failed: %s", e)
        if now - self._last_mark > timedelta(minutes=10):
            self._last_mark = now
            for p in all_rows(self.conn, "SELECT slug FROM positions WHERE status = 'open' AND qty > 0"):
                q = fetch_quote(self.client, p["slug"], with_bbo=False)
                if q is not None:
                    record_snapshot(self.conn, p["slug"], q, "poll", now)
            _mark_positions(self.conn, self.broker)

    def _refresh_live_orders(self) -> None:
        for r in all_rows(self.conn, "SELECT order_id, exchange_order_id FROM orders WHERE status IN ('submitted','open','partially_filled','cancel_requested') AND exchange_order_id IS NOT NULL"):
            try:
                bo = self.broker.get_order(r["exchange_order_id"])
            except Exception as e:
                log.warning("get_order failed: %s", e)
                continue
            if bo is not None:
                _apply_broker_order(self.conn, r["order_id"], bo)

    def daily_report(self, now: datetime) -> None:
        try:
            refresh_observed_settlements(self.conn, self.client, now)
            recompute(self.conn, self.cfg, now)
        except Exception as e:
            log.warning("calibration failed: %s", e)
        row = compute_daily_stats(self.conn, now - timedelta(days=1))
        png = render_daily_png(self.conn, self.settings.data_dir / "reports" / f"daily_{row['date']}_{self.settings.mode}.png", self.settings.mode)
        fields = [("Account value", money(D(row["end_equity"])), True), ("Change today", money(D(row["pnl"])), True), ("Orders placed / filled", f"{row['n_orders']} / {row['n_fills']}", True), ("Markets settled / won", f"{row['n_settled']} / {row['n_won']}", True)]
        self.post("daily", embeds=[embed(f"[{self.settings.mode.upper()}] Daily report for {row['date']}", "", "blue", fields)], file_path=png, dedupe_key=f"daily:{row['date']}")
        self._backup(now)

    def _backup(self, now: datetime) -> None:
        dest = self.settings.backups_dir / f"{self.settings.mode}_{now.strftime('%Y%m%d')}.db"
        try:
            import sqlite3
            b = sqlite3.connect(str(dest))
            self.conn.backup(b)
            b.close()
            for old in sorted(self.settings.backups_dir.glob(f"{self.settings.mode}_*.db"))[:-30]:
                old.unlink(missing_ok=True)
        except Exception as e:
            log.warning("backup failed: %s", e)

    def loop(self) -> None:
        self.announce_start()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        while not self.stop:
            now = now_utc()
            try:
                self.poll_tick(now)
                nxt = parse_iso(get_control(self.conn, "next_run_at", "") or None)
                run_now = get_control(self.conn, "run_now", "0") == "1"
                if run_now or nxt is None or now >= nxt:
                    set_control(self.conn, "run_now", "0")
                    self.run_once("discord" if run_now else "schedule")
                    set_control(self.conn, "next_run_at", iso(now_utc() + timedelta(hours=float(self.cfg.run_interval_hours))))
                self._maybe_daily(now)
            except Exception:
                log.exception("poll loop error")
            for _ in range(self.cfg.poll_seconds):
                if self.stop:
                    break
                time.sleep(1)
        log.info("runner stopped")

    def _maybe_daily(self, now: datetime) -> None:
        local = now.astimezone(local_tz())
        hh, mm = (int(x) for x in self.cfg.daily_report_time_local.split(":"))
        today = local.strftime("%Y-%m-%d")
        if get_control(self.conn, "last_daily_report", "") == today:
            return
        if (local.hour, local.minute) >= (hh, mm):
            set_control(self.conn, "last_daily_report", today)
            try:
                self.daily_report(now)
            except Exception:
                log.exception("daily report failed")

    def _on_signal(self, signum, frame) -> None:
        log.info("signal %s received, stopping after current step", signum)
        self.stop = True


def _git_sha() -> str | None:
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="runner")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--daily-report", action="store_true")
    args = ap.parse_args(argv)
    if args.dry_run:
        os.environ["PM_DRY_RUN"] = "1"
    settings = load_settings()
    cfg, cfg_hash = load_config()
    setup_logging(settings)
    r = Runner(settings, cfg, cfg_hash)
    if args.daily_report:
        r.daily_report(now_utc())
        return 0
    if args.once:
        run_id = r.run_once(args.trigger)
        row = one(r.conn, "SELECT status, summary_json FROM runs WHERE run_id = ?", (run_id,))
        print(dumps({"run_id": run_id, "status": row["status"], "summary": __import__("json").loads(row["summary_json"] or "{}")}, indent=2))
        return 0 if row["status"] in ("ok", "no_candidates") else 1
    r.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
