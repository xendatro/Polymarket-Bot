from scripts.common import boot, out


def main() -> None:
    settings, cfg, _, conn = boot()
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    out({"mode": settings.mode, "db": str(settings.db_path), "tables": n, "data_dir": str(settings.data_dir), "trading_enabled": conn.execute("SELECT value FROM control WHERE key = 'trading_enabled'").fetchone()[0]})


if __name__ == "__main__":
    main()
