import argparse

from pm.broker import make_broker
from pm.client import PMClient
from pm.execution import expire_orders, reconcile, snapshot_balance
from pm.settle import settle_positions
from pm.util import now_utc
from scripts.common import boot, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    settings, cfg, _, conn = boot()
    client = PMClient(settings)
    if args.check:
        res = {"mode": settings.mode, "db": str(settings.db_path), "credentials_present": client.has_credentials, "clock_skew_s": client.server_clock_skew_seconds()}
        sample = client.list_markets(max_items=1, active=True, closed=False)
        res["public_api_ok"] = bool(sample)
        if client.has_credentials:
            try:
                res["balances"] = client.balances()
                res["positions"] = client.positions()
                res["open_orders"] = len(client.open_orders())
                res["auth_ok"] = True
            except Exception as e:
                res["auth_ok"] = False
                res["auth_error"] = str(e)
        out(res)
        return
    broker = make_broker(settings, cfg, conn, client)
    now = now_utc()
    rec = reconcile(settings, cfg, conn, broker, now)
    settled = settle_positions(conn, client, cfg, settings.mode, now)
    expired = expire_orders(conn, broker, now)
    bal = snapshot_balance(conn, broker, "manual_sync")
    out({"reconcile": rec, "settled": settled, "expired": expired, "balance": bal})


if __name__ == "__main__":
    main()
