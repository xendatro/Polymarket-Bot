import argparse

from pm.broker import make_broker
from pm.client import PMClient
from pm.db import all_rows
from pm.execution import cancel_order
from scripts.common import boot, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("order_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--reason", default="manual cancel")
    args = ap.parse_args()
    settings, cfg, _, conn = boot()
    broker = make_broker(settings, cfg, conn, PMClient(settings))
    ids = [args.order_id] if args.order_id else []
    if args.all:
        ids = [r["order_id"] for r in all_rows(conn, "SELECT order_id FROM orders WHERE status IN ('open','partially_filled','submitted')")]
    out({oid: cancel_order(conn, broker, oid, args.reason) for oid in ids})


if __name__ == "__main__":
    main()
