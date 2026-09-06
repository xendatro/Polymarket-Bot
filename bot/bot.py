from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands, tasks

from pm.broker import make_broker
from pm.claude_runner import run_task
from pm.client import PMClient
from pm.config import REPO_ROOT, load_config, load_settings
from pm.db import all_rows, connect, get_control, init_schema, insert, log_event, set_control
from pm.execution import cancel_order, place_order
from pm.manual import build_manual_decision
from pm.notify import cents, embed, money, post_webhook, reasons_text, signed_money, status_word
from pm.report import status_summary
from pm.util import dstr, dumps, iso, now_utc, parse_iso

log = logging.getLogger("bot")
settings = load_settings()
cfg, cfg_hash = load_config()


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = TimedRotatingFileHandler(settings.logs_dir / "bot.log", when="midnight", backupCount=14, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def db():
    conn = connect(settings.db_path)
    init_schema(conn, settings.mode, cfg.paper.starting_cash)
    return conn


def is_owner(user: discord.abc.User) -> bool:
    return settings.discord_owner_id is not None and user.id == settings.discord_owner_id


def fmt_status(s: dict) -> str:
    eq = money(s["equity"]) if s["equity"] else "-"
    cash = money(s["cash"]) if s["cash"] else "-"
    trading = "on" if s["trading_enabled"] == "1" else f"OFF ({s['kill_reason'] or 'no reason recorded'})"
    from pm.notify import pct
    lines = [f"**Mode:** {s['mode']} | **Money:** {eq} total, {cash} cash, {pct(s.get('invested_pct'))} invested (target 50%)",
             f"**Trading:** {trading}" + (f" | paused until {s['paused_until']}" if s["paused_until"] else ""),
             f"**Settled markets:** {s['settled']} ({s['won']} won) | **Realized profit:** {money(s['realized_total'])}",
             f"**Next run:** {s['next_run_at'] or 'soon'} | **Runner last seen:** {s['last_heartbeat'] or 'never'}"]
    if s["last_run"]:
        lr = s["last_run"]
        lines.append(f"**Last run:** {lr['status']} at {lr['started_at']}")
    lines.append("**Open positions:** " + ("none" if not s["positions"] else ""))
    for p in s["positions"]:
        lines.append(f"- {p['slug']}: {p['qty']} {p['side']} bought at {cents(p['avg_cost'])}, now {cents(p['mark_price']) if p['mark_price'] else '-'} ({signed_money(p['unrealized_pnl']) if p['unrealized_pnl'] else '-'}), sells at {cents(p.get('take_profit_price'))} or {cents(p.get('stop_loss_price'))}")
    lines.append("**Open orders:** " + ("none" if not s["open_orders"] else ""))
    for o in s["open_orders"]:
        lines.append(f"- {o['slug']}: buy {o['qty']} {o['side']} at {cents(o['limit_price'])}, {o['filled_qty']} filled, {status_word(o['status'])}, expires {o['expires_at'] or '-'}")
    return "\n".join(lines)[:1900]


class ConfirmView(discord.ui.View):
    def __init__(self, proposal: dict, message_id: int):
        super().__init__(timeout=120)
        self.proposal = proposal
        self.message_id = message_id
        self.done = False

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction.user):
            await interaction.response.send_message("Only the owner can confirm trades.", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        result = await asyncio.to_thread(execute_proposal, self.proposal, self.message_id)
        await interaction.followup.send(result[:1900])

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)


def execute_proposal(proposal: dict, message_id: int) -> str:
    conn = db()
    try:
        client = PMClient(settings)
        broker = make_broker(settings, cfg, conn, client)
        d, market, quote, reasons = build_manual_decision(settings, cfg, cfg_hash, conn, client, proposal, now_utc(), source="discord")
        if not d.go or market is None:
            return f"Not placed, the risk checks failed: {reasons_text(reasons)}"
        res = place_order(settings, cfg, conn, broker, d, market, None, "discord", scope=f"discord-{message_id}")
        insert(conn, "discord_log", {"ts": iso(now_utc()), "user_id": str(settings.discord_owner_id), "user_name": "owner", "text": dumps(proposal), "reply": dumps(res.as_dict()), "action": "place_order"})
        if res.status in ("rejected", "dry_run"):
            return f"Not placed: {status_word(res.status)}. {reasons_text([res.reason]) if res.reason else ''}"
        post_webhook(settings.webhook_trades, embeds=[embed(f"[{settings.mode.upper()}] PLACED (discord) · {d.slug}", f"{d.side} {proposal.get('action')} x{d.proposed_qty} @ {dstr(d.proposed_price)} → {res.status}", "blue")], conn=conn, channel="trades")
        return f"Placed: buy {d.proposed_qty} {d.side} at {cents(d.proposed_price)} on {market.title or market.question}, now {status_word(res.status)}."
    finally:
        conn.close()


def preview_proposal(proposal: dict) -> tuple[str, bool]:
    conn = db()
    try:
        client = PMClient(settings)
        d, market, quote, reasons = build_manual_decision(settings, cfg, cfg_hash, conn, client, proposal, now_utc(), source="discord_preview")
        lines = [f"**{proposal.get('action', 'buy').upper()} {proposal.get('side')} x{proposal.get('qty')} @ {proposal.get('price')}** on `{proposal.get('slug')}`"]
        if market is not None:
            lines.append(market.question or market.title)
        if d.best_bid is not None:
            lines.append(f"side bid/ask now {dstr(d.best_bid)} / {dstr(d.best_ask)} · fee/contract {dstr(d.fee_per_contract, 4)}")
        lines.append(f"rationale: {proposal.get('rationale', '')[:300]}")
        lines.append("Risk checks: " + ("all passed" if d.go else "FAILED: " + reasons_text(reasons)))
        return "\n".join(lines)[:1900], d.go
    finally:
        conn.close()


def answer_query(message: str, user_id: str, user_name: str) -> dict:
    conn = db()
    try:
        state = status_summary(conn, settings.mode)
        ctx = {"today": now_utc().strftime("%Y-%m-%d"), "mode": settings.mode, "message": message, "state": state}
        res = run_task(settings, cfg, conn, "query", ctx, run_id=None, slug=None, max_turns=cfg.claude.query_max_turns, allowed_tools=["Read", "WebSearch", "WebFetch", "Bash(python -m scripts.ro.*)"], disallowed_tools=["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent"], cwd=REPO_ROOT / "ask", enforce_budget=False)
        out = res.output if res.ok and res.output else {"reply": f"Sorry, the assistant call failed: {res.error[:300]}", "proposal": None}
        insert(conn, "discord_log", {"ts": iso(now_utc()), "user_id": user_id, "user_name": user_name, "text": message, "reply": out.get("reply"), "action": "proposal" if out.get("proposal") else "reply", "payload_json": dumps(out.get("proposal"))})
        return out
    finally:
        conn.close()


def control_action(action: str, reason: str = "") -> str:
    conn = db()
    try:
        if action == "kill":
            set_control(conn, "trading_enabled", "0")
            set_control(conn, "kill_reason", reason or "discord /kill")
            log_event(conn, "discord", "warn", "kill_set", None, None, {"reason": reason})
            try:
                broker = make_broker(settings, cfg, conn, PMClient(settings))
                for r in all_rows(conn, "SELECT order_id FROM orders WHERE status IN ('open','partially_filled','submitted')"):
                    cancel_order(conn, broker, r["order_id"], "kill switch")
            except Exception as e:
                return f"Trading halted, but cancelling open orders failed: {e}"
            return f"Trading halted ({reason or 'no reason given'}); open orders cancelled, positions kept."
        if action == "resume":
            set_control(conn, "trading_enabled", "1")
            set_control(conn, "kill_reason", "")
            set_control(conn, "paused_until", "")
            set_control(conn, "pause_reason", "")
            log_event(conn, "discord", "info", "trading_enabled", None, None, {"mode": settings.mode})
            return "Trading enabled."
        if action == "arm":
            if not settings.is_live:
                return "Runner is in paper mode; /arm only applies when PM_MODE=live."
            set_control(conn, "trading_enabled", "1")
            set_control(conn, "kill_reason", "")
            set_control(conn, "live_armed_at", iso(now_utc()))
            log_event(conn, "discord", "warn", "live_armed", None, None, None)
            return "LIVE trading armed. Real money is at risk."
        if action == "run_now":
            set_control(conn, "run_now", "1")
            return "Run requested; the runner will start it within a minute."
        return "unknown action"
    finally:
        conn.close()


def status_text() -> str:
    conn = db()
    try:
        return fmt_status(status_summary(conn, settings.mode))
    finally:
        conn.close()


def cancel_action(order_id: str) -> str:
    conn = db()
    try:
        broker = make_broker(settings, cfg, conn, PMClient(settings))
        rows = all_rows(conn, "SELECT order_id FROM orders WHERE order_id LIKE ? AND status IN ('open','partially_filled','submitted')", (order_id + "%",))
        if not rows:
            return "No open order matches that id."
        ok = [cancel_order(conn, broker, r["order_id"], "discord /cancel") for r in rows]
        return f"Cancel requested for {sum(1 for o in ok if o)} order(s)."
    finally:
        conn.close()


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("bot ready as %s", bot.user)
    if settings.discord_guild_id:
        guild = discord.Object(id=settings.discord_guild_id)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    if not watchdog.is_running():
        watchdog.start()
    post_webhook(settings.webhook_trades, embeds=[embed(f"[{settings.mode.upper()}] Discord bot online", "/status · /kill · /resume · /run_now · #ask", "purple")])


@bot.tree.command(name="status", description="Balances, positions, open orders, control flags")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(await asyncio.to_thread(status_text))


@bot.tree.command(name="kill", description="Halt trading and cancel open orders")
@app_commands.describe(reason="Why")
async def kill_cmd(interaction: discord.Interaction, reason: str = ""):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(await asyncio.to_thread(control_action, "kill", reason))


@bot.tree.command(name="resume", description="Re-enable trading after a halt or pause")
async def resume_cmd(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.send_message(await asyncio.to_thread(control_action, "resume"))


@bot.tree.command(name="arm", description="Arm LIVE trading (only when PM_MODE=live)")
async def arm_cmd(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.send_message(await asyncio.to_thread(control_action, "arm"))


@bot.tree.command(name="run_now", description="Trigger a trading run as soon as possible")
async def run_now_cmd(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.send_message(await asyncio.to_thread(control_action, "run_now"))


@bot.tree.command(name="cancel", description="Cancel an open order by id prefix")
async def cancel_cmd(interaction: discord.Interaction, order_id: str):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(await asyncio.to_thread(cancel_action, order_id))


@bot.tree.command(name="mode", description="Show the runner mode and data location")
async def mode_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"mode={settings.mode} dry_run={settings.dry_run} db={settings.db_path.name} interval={cfg.run_interval_hours}h")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if settings.discord_ask_channel_id and message.channel.id == settings.discord_ask_channel_id:
        if not is_owner(message.author):
            return
        async with message.channel.typing():
            out = await asyncio.to_thread(answer_query, message.content, str(message.author.id), message.author.name)
        reply = out.get("reply") or "(no reply)"
        for i in range(0, len(reply), 1900):
            await message.channel.send(reply[i:i + 1900])
        proposal = out.get("proposal")
        if proposal:
            text, ok = await asyncio.to_thread(preview_proposal, proposal)
            view = ConfirmView(proposal, message.id) if ok else None
            await message.channel.send(text, view=view)
        return
    await bot.process_commands(message)


@tasks.loop(minutes=5)
async def watchdog():
    def check() -> str | None:
        conn = db()
        try:
            hb = parse_iso(get_control(conn, "last_heartbeat", "") or None)
            if hb is None:
                return None
            age = now_utc() - hb
            if age > timedelta(minutes=15):
                key = f"hb_stale:{now_utc().strftime('%Y%m%d%H')}"
                post_webhook(settings.webhook_alerts, embeds=[embed("Runner may be down", f"The trading runner has not checked in for {int(age.total_seconds() // 60)} minutes (last seen {iso(hb)}). Nothing is being traded or monitored until it is back.", "red")], conn=conn, channel="alerts", dedupe_key=key)
                return "stale"
            return None
        finally:
            conn.close()
    await asyncio.to_thread(check)


def main() -> int:
    setup_logging()
    if not settings.discord_token:
        print("DISCORD_BOT_TOKEN missing in .env")
        return 1
    bot.run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
