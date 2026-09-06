import sys

from pm.client import PMClient, market_quotes
from pm.util import dstr
from scripts.common import boot, out


def main() -> None:
    text = " ".join(sys.argv[1:]).strip().lower()
    if not text:
        out({"error": "usage: python -m scripts.ro.search <text>"})
        return
    settings, cfg, _, conn = boot(readonly=True)
    client = PMClient()
    hits = []
    for cat in cfg.scan.categories + ["sports", "crypto"]:
        for m in client.list_markets(max_items=500, active=True, closed=False, categories=[cat]):
            hay = f"{m.get('question', '')} {m.get('title', '')} {m.get('slug', '')}".lower()
            if text in hay:
                b, a = market_quotes(m)
                hits.append({"slug": m.get("slug"), "question": m.get("question"), "title": m.get("title"), "category": cat, "end_date": m.get("endDate"), "yes_bid": dstr(b), "yes_ask": dstr(a)})
        if len(hits) >= 25:
            break
    out({"query": text, "hits": hits[:25]})


if __name__ == "__main__":
    main()
