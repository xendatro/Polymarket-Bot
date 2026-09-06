import argparse
from decimal import Decimal

from pm.broker import make_broker
from pm.client import PMClient
from pm.manual import build_manual_decision
from pm.execution import place_order
from pm.util import now_utc
from scripts.common import boot, out


def main() -> None:
    ap = argparse.ArgumentParser(description="Manually place an order through the same guarded path the runner uses.")
    ap.add_argument("slug")
    ap.add_argument("side", choices=["YES", "NO"])
    ap.add_argument("action", choices=["buy", "sell"])
    ap.add_argument("price", type=Decimal)
    ap.add_argument("qty", type=int)
    ap.add_argument("--reason", default="manual cli")
    args = ap.parse_args()
    settings, cfg, cfg_hash, conn = boot()
    client = PMClient(settings)
    broker = make_broker(settings, cfg, conn, client)
    d, market, quote, reasons = build_manual_decision(settings, cfg, cfg_hash, conn, client, {"slug": args.slug, "side": args.side, "action": args.action, "price": str(args.price), "qty": args.qty, "rationale": args.reason}, now_utc(), source="cli")
    if not d.go:
        out({"placed": False, "reasons": reasons, "decision_id": d.decision_id})
        return
    res = place_order(settings, cfg, conn, broker, d, market, None, "cli", scope=f"cli-{now_utc().strftime('%Y%m%d%H%M%S')}")
    out({"placed": res.status not in ("rejected", "dry_run"), "result": res.as_dict()})


if __name__ == "__main__":
    main()
