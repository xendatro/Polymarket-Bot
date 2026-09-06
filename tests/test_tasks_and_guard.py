import json
import subprocess
import sys

import jsonschema

from pm.claude_runner import load_task, render
from pm.config import REPO_ROOT


def test_task_schemas_are_valid_and_render():
    for task in ("query",):
        prompt, schema = load_task(task)
        jsonschema.Draft202012Validator.check_schema(schema)
        assert "{{" in prompt
    assert render("a {{x}} b", {"x": "Y"}) == "a Y b"


def _guard(cmd: str) -> int:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    p = subprocess.run([sys.executable, str(REPO_ROOT / ".claude" / "hooks" / "guard_bash.py")], input=payload, capture_output=True, text=True)
    return p.returncode


def test_guard_allows_only_readonly_scripts():
    assert _guard("python -m scripts.ro.status") == 0
    assert _guard('python -m scripts.ro.query_db "SELECT 1"') == 0
    assert _guard("python -m scripts.place_order x YES buy 0.5 1") == 2
    assert _guard('python -c "import polymarket_us"') == 2
    assert _guard("curl https://api.polymarket.us/v1/orders") == 2
    assert _guard("python -m scripts.ro.status | cat") == 2
    assert _guard("cat .env") == 2
    assert _guard('cd "C:/repo/path" && python -m scripts.ro.recent 3') == 0
    assert _guard("cd /x && python -m scripts.ro.status && rm -rf /") == 2
