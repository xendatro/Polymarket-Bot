import argparse

from pm.db import get_control, log_event, set_control
from pm.util import iso, now_utc
from scripts.common import boot, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["on", "off", "arm", "status", "pause", "unpause", "run-now"])
    ap.add_argument("reason", nargs="?", default="manual")
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    settings, cfg, _, conn = boot()
    if args.action == "on":
        set_control(conn, "trading_enabled", "0")
        set_control(conn, "kill_reason", args.reason)
        log_event(conn, "human", "warn", "kill_set", None, None, {"reason": args.reason})
    elif args.action in ("off", "arm"):
        set_control(conn, "trading_enabled", "1")
        set_control(conn, "kill_reason", "")
        if settings.is_live:
            set_control(conn, "live_armed_at", iso(now_utc()))
        log_event(conn, "human", "info", "trading_enabled", None, None, {"mode": settings.mode})
    elif args.action == "pause":
        from datetime import timedelta
        set_control(conn, "paused_until", iso(now_utc() + timedelta(hours=args.hours)))
        set_control(conn, "pause_reason", args.reason)
    elif args.action == "unpause":
        set_control(conn, "paused_until", "")
        set_control(conn, "pause_reason", "")
    elif args.action == "run-now":
        set_control(conn, "run_now", "1")
    out({k: get_control(conn, k) for k in ("trading_enabled", "kill_reason", "paused_until", "pause_reason", "run_now", "next_run_at", "last_heartbeat", "claude_paused_until")} | {"mode": settings.mode})


if __name__ == "__main__":
    main()
