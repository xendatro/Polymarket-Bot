from __future__ import annotations

import sys

from pm.config import load_config, load_settings
from pm.db import connect, init_schema
from pm.util import dumps

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def boot(readonly: bool = False):
    settings = load_settings()
    cfg, cfg_hash = load_config()
    if readonly:
        if not settings.db_path.exists():
            raise SystemExit(f"database not found: {settings.db_path} (run python -m scripts.init_db)")
        conn = connect(settings.db_path, readonly=True)
    else:
        conn = connect(settings.db_path)
        init_schema(conn, settings.mode, cfg.paper.starting_cash)
    return settings, cfg, cfg_hash, conn


def out(obj) -> None:
    print(dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)
