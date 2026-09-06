import sqlite3

from pm.util import now_utc
from scripts.common import boot, out


def main() -> None:
    settings, cfg, _, conn = boot()
    dest = settings.backups_dir / f"{settings.mode}_{now_utc().strftime('%Y%m%d_%H%M%S')}.db"
    b = sqlite3.connect(str(dest))
    conn.backup(b)
    b.close()
    out({"backup": str(dest)})


if __name__ == "__main__":
    main()
