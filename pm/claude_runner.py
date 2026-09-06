from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import jsonschema
import psutil

from pm.config import REPO_ROOT, Config, Settings
from pm.db import get_control, insert, log_event, scalar, set_control
from pm.util import dumps, iso, local_tz, now_utc, parse_iso, read_text, write_text

TASKS_DIR = REPO_ROOT / "tasks"
HEADLESS_SETTINGS = REPO_ROOT / ".claude" / "headless-settings.json"
SCRUB_PREFIXES = ("POLYMARKET_", "DISCORD_", "HEALTHCHECK_", "PM_")


class ClaudeUnavailable(Exception):
    pass


class TaskResult:
    def __init__(self, ok: bool, output: dict | None, envelope: dict | None, error: str = "", call_id: int | None = None):
        self.ok = ok
        self.output = output
        self.envelope = envelope or {}
        self.error = error
        self.call_id = call_id

    @property
    def usage(self) -> dict:
        return self.envelope.get("usage") or {}


def load_task(name: str) -> tuple[str, dict]:
    prompt = read_text(TASKS_DIR / f"{name}.md")
    schema = json.loads(read_text(TASKS_DIR / f"{name}.schema.json"))
    return prompt, schema


def render(template: str, context: dict) -> str:
    out = template
    for k, v in context.items():
        val = v if isinstance(v, str) else dumps(v, indent=2)
        out = out.replace("{{" + k + "}}", val)
    return out


def resolve_claude_command() -> list[str]:
    exe = shutil.which("claude")
    if not exe:
        for cand in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude", Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"):
            if cand.exists():
                exe = str(cand)
                break
    if not exe:
        raise ClaudeUnavailable("claude executable not found on PATH")
    if exe.lower().endswith(".cmd"):
        text = Path(exe).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if "cli.js" in line:
                start = line.find('"')
                js = None
                parts = line.replace('"', " ").split()
                for p in parts:
                    if p.endswith("cli.js"):
                        js = p.replace("%~dp0", str(Path(exe).parent) + os.sep)
                        break
                if js and Path(js).exists():
                    node = shutil.which("node") or "node"
                    return [node, js]
    return [exe]


def scrubbed_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SCRUB_PREFIXES)}
    env["PYTHONIOENCODING"] = "utf-8"
    venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env["PM_MODE"] = os.environ.get("PM_MODE", "paper")
    if os.environ.get("PM_DATA_DIR"):
        env["PM_DATA_DIR"] = os.environ["PM_DATA_DIR"]
    return env



def effective_settings_path(settings: Settings) -> Path:
    base = json.loads(read_text(HEADLESS_SETTINGS))
    guard = REPO_ROOT / ".claude" / "hooks" / "guard_bash.py"
    cmd = f'"{sys.executable}" "{guard}"'
    for group in (base.get("hooks") or {}).get("PreToolUse", []):
        for h in group.get("hooks", []):
            if h.get("type") == "command" and "guard_bash.py" in (h.get("command") or ""):
                h["command"] = cmd
    out = settings.data_dir / "headless-settings.effective.json"
    text = json.dumps(base, indent=2)
    if not out.exists() or out.read_text(encoding="utf-8") != text:
        write_text(out, text)
    return out


def claude_budget_ok(conn: sqlite3.Connection, cfg: Config, now: datetime) -> tuple[bool, str]:
    paused = parse_iso(get_control(conn, "claude_paused_until", "") or None)
    if paused is not None and paused > now:
        return False, f"claude_paused_until_{iso(paused)}"
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    n = int(scalar(conn, "SELECT COUNT(*) FROM claude_calls WHERE created_at >= ? AND status != 'skipped'", (day_start,), 0))
    if n >= cfg.claude.max_calls_per_day:
        return False, f"claude_daily_budget_{n}/{cfg.claude.max_calls_per_day}"
    local_hour = now.astimezone(local_tz()).hour
    if local_hour in cfg.claude.quiet_hours_local:
        return False, f"quiet_hour_{local_hour}"
    return True, ""


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
    except psutil.Error:
        try:
            proc.kill()
        except Exception:
            pass


def run_task(settings: Settings, cfg: Config, conn: sqlite3.Connection, task: str, context: dict, run_id: str | None = None, slug: str | None = None, max_turns: int | None = None, allowed_tools: list[str] | None = None, disallowed_tools: list[str] | None = None, cwd: Path | None = None, model: str | None = None, enforce_budget: bool = True) -> TaskResult:
    now = now_utc()
    if enforce_budget:
        ok, why = claude_budget_ok(conn, cfg, now)
        if not ok:
            cid = insert(conn, "claude_calls", {"run_id": run_id, "task": task, "slug": slug, "model": model or cfg.claude.model, "status": "skipped", "error_text": why, "created_at": iso(now)})
            return TaskResult(False, None, None, why, cid)
    template, schema = load_task(task)
    prompt = render(template, context)
    model = model or cfg.claude.model
    turns = max_turns or cfg.claude.query_max_turns
    cmd = resolve_claude_command() + ["-p", "--model", model, "--output-format", "json", "--json-schema", json.dumps(schema, separators=(",", ":")), "--max-turns", str(turns), "--permission-mode", "dontAsk", "--permission-prompts", "none", "--strict-mcp-config", "--settings", str(effective_settings_path(settings))]
    allowed = allowed_tools if allowed_tools is not None else ["WebSearch", "WebFetch"]
    disallowed = disallowed_tools if disallowed_tools is not None else ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Read", "Glob", "Grep"]
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        cmd += ["--disallowedTools", ",".join(disallowed)]
    workdir = str(cwd or REPO_ROOT)
    started = time.monotonic()
    raw_dir = settings.runs_dir / (run_id or "adhoc")
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    raw_path = raw_dir / f"{task}_{(slug or 'none')[:40]}_{stamp}.json"
    write_text(raw_dir / f"{task}_{(slug or 'none')[:40]}_{stamp}.prompt.md", prompt)
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, env=scrubbed_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise ClaudeUnavailable(str(e)) from e
    try:
        stdout, stderr = proc.communicate(prompt, timeout=cfg.claude.timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = "", "timeout"
        duration = int((time.monotonic() - started) * 1000)
        cid = insert(conn, "claude_calls", {"run_id": run_id, "task": task, "slug": slug, "model": model, "status": "timeout", "duration_ms": duration, "error_text": f"timeout after {cfg.claude.timeout_seconds}s", "raw_path": str(raw_path), "created_at": iso(now)})
        log_event(conn, "claude", "warn", "claude_timeout", "claude_calls", cid, {"task": task, "slug": slug})
        return TaskResult(False, None, None, "timeout", cid)
    duration = int((time.monotonic() - started) * 1000)
    write_text(raw_path, stdout or "")
    if stderr:
        write_text(raw_path.with_suffix(".stderr.txt"), stderr)
    envelope: dict | None = None
    try:
        envelope = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        envelope = None
    if envelope is None:
        err = (stderr or "").strip()[-500:] or f"exit {proc.returncode}, no json"
        cid = insert(conn, "claude_calls", {"run_id": run_id, "task": task, "slug": slug, "model": model, "status": "error", "duration_ms": duration, "error_text": err, "raw_path": str(raw_path), "created_at": iso(now)})
        _maybe_pause_for_limits(conn, err, now)
        return TaskResult(False, None, None, err, cid)
    usage = envelope.get("usage") or {}
    status = "ok"
    error = ""
    output = envelope.get("structured_output")
    if envelope.get("is_error") or output is None:
        status = "error"
        errs = envelope.get("errors") or []
        error = "; ".join(str(e) for e in errs) or str(envelope.get("result") or envelope.get("subtype") or "no structured output")[:500]
    else:
        try:
            jsonschema.validate(output, schema)
        except jsonschema.ValidationError as e:
            status = "invalid"
            error = f"schema: {e.message}"[:500]
            output = None
    cid = insert(conn, "claude_calls", {
        "run_id": run_id, "task": task, "slug": slug, "model": model, "status": status, "session_id": envelope.get("session_id"), "num_turns": envelope.get("num_turns"),
        "input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0), "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0), "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cost_usd": str(envelope.get("total_cost_usd") or "0"), "duration_ms": duration, "error_text": error or None, "raw_path": str(raw_path), "created_at": iso(now),
    })
    if status != "ok":
        _maybe_pause_for_limits(conn, error, now)
        log_event(conn, "claude", "warn", f"claude_{status}", "claude_calls", cid, {"task": task, "slug": slug, "error": error})
    return TaskResult(status == "ok", output, envelope, error, cid)


def _maybe_pause_for_limits(conn: sqlite3.Connection, err: str, now: datetime) -> None:
    e = (err or "").lower()
    if any(k in e for k in ("rate limit", "rate_limit", "usage limit", "limit reached", "overloaded", "429", "billing", "out of extra usage", "exceeded")):
        until = now + timedelta(hours=1)
        set_control(conn, "claude_paused_until", iso(until))
        log_event(conn, "claude", "warn", "claude_paused", None, None, {"until": iso(until), "reason": err[:200]})


def claude_usage_today(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or now_utc()
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens + cache_read_tokens + cache_write_tokens), 0) AS inp, COALESCE(SUM(output_tokens), 0) AS out, COALESCE(SUM(CAST(cost_usd AS REAL)), 0) AS cost FROM claude_calls WHERE created_at >= ? AND status != 'skipped'", (day_start,)).fetchone()
    return {"calls": int(r["n"]), "input_tokens": int(r["inp"]), "output_tokens": int(r["out"]), "cost_usd_estimate": round(float(r["cost"]), 4)}
