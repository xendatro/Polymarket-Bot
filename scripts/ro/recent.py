import sys

from pm.db import rows_to_dicts
from scripts.common import boot, out


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    settings, cfg, _, conn = boot(readonly=True)
    out({
        "runs": rows_to_dicts(conn.execute("SELECT run_id, started_at, finished_at, status, trigger, candidates_scanned, candidates_researched, decisions, orders_placed, claude_calls, error_text FROM runs ORDER BY started_at DESC LIMIT ?", (n,)).fetchall()),
        "decisions": rows_to_dicts(conn.execute("SELECT created_at, slug, side, proposed_price, proposed_qty, tier, edge_net, go, nogo_reasons_json, rationale FROM decisions ORDER BY decision_id DESC LIMIT ?", (n * 3,)).fetchall()),
        "orders": rows_to_dicts(conn.execute("SELECT created_at, slug, side, intent, limit_price, qty, filled_qty, avg_fill_price, status, source, error_text FROM orders ORDER BY created_at DESC LIMIT ?", (n * 2,)).fetchall()),
        "settlements": rows_to_dicts(conn.execute("SELECT detected_at, slug, side, qty_held, realized_pnl, won FROM settlements ORDER BY settlement_id DESC LIMIT ?", (n,)).fetchall()),
    })


if __name__ == "__main__":
    main()
