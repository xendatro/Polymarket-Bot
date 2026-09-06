import argparse
from datetime import timedelta

from pm.notify import embed, money, post_webhook
from pm.report import compute_daily_stats, render_daily_png
from pm.util import D, now_utc
from scripts.common import boot, out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--today", action="store_true")
    args = ap.parse_args()
    settings, cfg, _, conn = boot()
    day = now_utc() if args.today else now_utc() - timedelta(days=1)
    row = compute_daily_stats(conn, day)
    png = render_daily_png(conn, settings.data_dir / "reports" / f"daily_{row['date']}_{settings.mode}.png", settings.mode)
    if args.post:
        fields = [("Equity", money(D(row["end_equity"])), True), ("Day P&L", money(D(row["pnl"])), True), ("Settled / won", f"{row['n_settled']} / {row['n_won']}", True)]
        post_webhook(settings.webhook_daily, embeds=[embed(f"[{settings.mode.upper()}] daily report {row['date']}", "", "blue", fields)], file_path=png, conn=conn, channel="daily")
    out({"stats": row, "png": str(png)})


if __name__ == "__main__":
    main()
