import re
import sys

from pm.db import rows_to_dicts
from scripts.common import boot, out

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b", re.IGNORECASE)


def main() -> None:
    sql = " ".join(sys.argv[1:]).strip().rstrip(";")
    if not sql.lower().startswith(("select", "with")) or FORBIDDEN.search(sql) or ";" in sql:
        out({"error": "only a single SELECT statement is allowed"})
        return
    settings, cfg, _, conn = boot(readonly=True)
    try:
        rows = conn.execute(sql).fetchmany(200)
    except Exception as e:
        out({"error": str(e)})
        return
    out({"rows": rows_to_dicts(rows), "truncated": len(rows) == 200})


if __name__ == "__main__":
    main()
