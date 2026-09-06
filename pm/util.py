from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, tail = s.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in tail:
                idx = tail.index(sep)
                tz = tail[idx:]
                tail = tail[:idx]
                break
        s = f"{head}.{tail[:6]}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def D(x: Any, default: Decimal | None = None) -> Decimal | None:
    if x is None or x == "":
        return default
    if isinstance(x, Decimal):
        return x
    if isinstance(x, dict) and "value" in x:
        return D(x["value"], default)
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return default


def dstr(x: Decimal | None, places: int = 4) -> str | None:
    if x is None:
        return None
    q = Decimal(1).scaleb(-places)
    v = x.quantize(q, rounding=ROUND_HALF_EVEN)
    if v == 0:
        return "0"
    return format(v.normalize(), "f")


def round_to_tick(price: Decimal, tick: Decimal, rounding=ROUND_DOWN) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_EVEN)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return iso(o)
        if isinstance(o, Path):
            return str(o)
        return super().default(o)


def dumps(obj: Any, **kw) -> str:
    return json.dumps(obj, cls=DecimalEncoder, ensure_ascii=False, **kw)


def loads(s: str | bytes | None, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


def sha256_short(s: str, n: int = 32) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def read_text(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: Path | str, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def local_tz():
    name = os.environ.get("PM_TIMEZONE")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def hours_between(a: datetime, b: datetime) -> Decimal:
    return Decimal((b - a).total_seconds()) / Decimal(3600)


def plus_hours(dt: datetime, hours: float | Decimal) -> datetime:
    return dt + timedelta(hours=float(hours))


def clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, x))
