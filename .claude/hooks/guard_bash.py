import json
import re
import sys

ALLOWED = re.compile(r"^\s*(?:cd\s+(?:\"[^\"&|;<>`$]+\"|'[^'&|;<>`$]+'|[^\s&|;<>`$]+)\s*&&\s*)?(?:\S*python(?:3|\.exe)?\s+)-m\s+scripts\.ro\.[a-z_]+(?:\s+[^;&|<>`$]*)?\s*$")
FORBIDDEN = re.compile(r"(polymarket_us|orders\.create|api\.polymarket\.us|python\s+-c|\.env|\bcurl\b|\bwget\b|\brm\b|\bdel\b|>>?|\|)", re.IGNORECASE)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("guard: unreadable hook input", file=sys.stderr)
        return 2
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if FORBIDDEN.search(cmd) or not ALLOWED.match(cmd):
        print("guard: only `python -m scripts.ro.<name> [args]` is permitted in this repository", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
