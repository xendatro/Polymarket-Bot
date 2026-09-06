import sys

from pm.client import PMClient, book_summary
from pm.util import dstr
from scripts.common import boot, out


def main() -> None:
    if len(sys.argv) < 2:
        out({"error": "usage: python -m scripts.ro.market <slug>"})
        return
    slug = sys.argv[1]
    settings, cfg, _, conn = boot(readonly=True)
    client = PMClient()
    m = client.get_market(slug)
    if m is None:
        out({"error": f"market {slug} not found"})
        return
    s = book_summary(client.get_book(slug))
    bbo = client.get_bbo(slug) or {}
    notes = conn.execute("SELECT created_at, task, p_yes, confidence, recommendation, summary FROM research_notes WHERE slug = ? ORDER BY note_id DESC LIMIT 3", (slug,)).fetchall()
    out({
        "slug": slug, "question": m.get("question"), "title": m.get("title"), "category": m.get("category"), "status": m.get("status"), "end_date": m.get("endDate"),
        "description": m.get("description"), "tick": m.get("orderPriceMinTickSize"), "fee_coefficient": m.get("feeCoefficient"), "min_qty": m.get("minimumTradeQty"),
        "yes_bid": dstr(s["yes_bid"]), "yes_ask": dstr(s["yes_ask"]), "no_bid": dstr(1 - s["yes_ask"]) if s["yes_ask"] is not None else None, "no_ask": dstr(1 - s["yes_bid"]) if s["yes_bid"] is not None else None,
        "bids": [[dstr(p), dstr(q)] for p, q in s["bids"]], "asks": [[dstr(p), dstr(q)] for p, q in s["asks"]], "open_interest": bbo.get("openInterest"), "last_trade": (bbo.get("lastTradePx") or {}).get("value"),
        "recent_research": [dict(n) for n in notes],
    })


if __name__ == "__main__":
    main()
