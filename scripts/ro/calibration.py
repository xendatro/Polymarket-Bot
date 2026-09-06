from pm.db import rows_to_dicts
from scripts.common import boot, out


def main() -> None:
    settings, cfg, _, conn = boot(readonly=True)
    rows = rows_to_dicts(conn.execute("SELECT category, price_band, n, mean_residual, bias_est, updated_at FROM calibration ORDER BY category, price_band").fetchall())
    observed = conn.execute("SELECT COUNT(DISTINCT c.slug) FROM candidates c JOIN markets m ON m.slug = c.slug WHERE c.strategy = 'favorites' AND m.settlement_price IS NOT NULL").fetchone()[0]
    out({"prior_bias": str(cfg.favorites.bias), "min_n_to_use": cfg.calibration.min_n_to_use, "observed_settled_favorites": observed, "buckets": rows})


if __name__ == "__main__":
    main()
