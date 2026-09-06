# Polymarket agent

An automated paper/live trading loop for **Polymarket US**. Every hour a pure-Python runner checks open positions (take-profit, stop-loss), scans the market list for liquid favorites settling within a week, sizes them with hard code-enforced limits, places orders (paper or live), logs everything to SQLite, and posts a Trade Report to Discord. A Discord bot (the only place Claude is used) answers questions and lets you confirm manual trades with a button.

The strategy (pure rules, no LLM in the trading loop): keep half of total money (cash plus the current value of positions) as a cash reserve and put the other half into **favorites**, contracts priced 65 to 90 cents on active markets that settle within a week. This exploits the documented favorite-longshot bias on prediction markets (favorites win slightly more often than their price implies). Eligible markets are ranked by `score = (bias / price) / days_to_settle * liquidity - 10 * spread` (bias 0.025, liquidity = open interest / $2,000 capped at 1) and bought in score order until the 50% ceiling, at most 20% of total money per bet and never two bets on the same game or question. Positions are managed hourly by simple rules: sell when the price rises 8 cents above entry (or reaches 95 cents), sell to limit damage when it falls 20 cents below entry, and never touch a sports position from an hour before kickoff until the game settles. Claude is used only for the Discord assistant in #ask.

## How it fits together

```
runner (python -m runner)        every hour
  reconcile fills -> settle resolved markets -> expire stale orders
  manage.py: sell at take-profit / stop-loss (sports frozen during games)
  rules.py: fetch markets, keep favorites (65-90c, tight spread, liquid, settles within 7d), score, buy toward 50%
  Discord: Trade Report card -> #trades, Current Trades card edited in place -> #runs
bot (python -m bot)              slash commands, #ask channel -> claude -p query task -> reply + confirm buttons
dashboard (streamlit)            read-only view of the SQLite database
```

Claude never holds credentials: the `claude -p` subprocess runs with `POLYMARKET_*` and `DISCORD_*` stripped from its environment, with Write/Edit/Bash disallowed (the #ask assistant gets a read-only Bash allowlist guarded by a PreToolUse hook), and its only output is JSON validated against a schema. Python owns sizing, limits, and order placement.

## Requirements

- Python 3.10+ (3.12 recommended)
- Node 18+ and Claude Code (`npm i -g @anthropic-ai/claude-code`), logged in with your Claude subscription (`claude` then `/login`)
- A Polymarket US account that has passed identity verification in the app
- A Discord server you administer

## Install

### Ubuntu 24 (recommended host)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip python-is-python3 git
git clone <your-repo-url> ~/polymarket-agent && cd ~/polymarket-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### Windows 11

```powershell
winget install Python.Python.3.12
git clone <your-repo-url> $HOME\polymarket-agent; cd $HOME\polymarket-agent
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

Then, on either OS, run `claude` once inside the repo directory and accept the workspace trust prompt, then exit. This lets the headless calls use the project settings.

## Configure `.env`

| Variable | Where to get it |
|---|---|
| `PM_MODE` | `paper` (default) or `live` |
| `PM_DRY_RUN` | `1` runs the whole pipeline but places no orders, not even paper ones |
| `PM_DATA_DIR` | Folder for the databases, logs, and raw Claude outputs. Keep it **out of OneDrive/Dropbox/iCloud**; a synced SQLite file corrupts. Defaults to `./data`. |
| `PM_TIMEZONE` | Optional IANA zone for the daily report time, e.g. `America/New_York` |
| `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY` | Finish KYC in the Polymarket US app, sign in at `polymarket.us/developer` **with the same login method** (Apple/Google/email) and create a key. The secret is shown once. |
| `DISCORD_BOT_TOKEN` | `discord.com/developers/applications`: New Application, Bot, Reset Token. Enable the **Message Content Intent**. Invite with the OAuth2 URL Generator: scopes `bot` + `applications.commands`, permissions Send Messages, Embed Links, Attach Files, Read Message History. |
| `DISCORD_GUILD_ID`, `DISCORD_OWNER_ID`, `DISCORD_ASK_CHANNEL_ID` | Discord Settings, Advanced, Developer Mode; then right-click the server, your name, and the #ask channel and Copy ID |
| `DISCORD_WEBHOOK_RUNS/TRADES/DAILY/ALERTS` | In each channel: Edit Channel, Integrations, Webhooks, New Webhook, Copy URL |
| `HEALTHCHECK_URL` | Optional. Create a check at healthchecks.io with a period of `run_interval_hours` and paste the ping URL. |

Market data is public, so the runner works in paper mode with no Polymarket key. The key is needed for balances, positions, and live orders.

## First run

```bash
python -m scripts.init_db             # creates <PM_DATA_DIR>/paper.db
python -m scripts.sync --check        # public API, clock skew, and (if keys are set) auth + balances
python -m scripts.ro.funnel           # how many markets pass each filter right now
python -m runner --once               # one full hourly cycle (paper)
pytest                                # unit tests
```

`--once` prints the run summary as JSON and posts it to #runs if the webhook is set. Raw prompts and Claude outputs for every call are saved under `<PM_DATA_DIR>/runs/<run_id>/`.

## Run it 24/7

Ubuntu: `deploy/install_ubuntu.sh` installs three systemd units (runner, bot, dashboard) for the current user, disables lid-switch suspend, and enables NTP. Logs: `journalctl -u polymarket-runner -f`. Also disable automatic suspend in Settings > Power and keep the laptop on AC.

Windows: see `deploy/windows.md` (NSSM services) or `deploy\start_windows.bat` for three console windows.

The dashboard listens on port 8501; open `http://<laptop-ip>:8501` from another machine on the same network (or over Tailscale).

## Discord

| Channel | What lands there |
|---|---|
| #runs | one "Current Trades" card, edited in place every hour: every open position and resting order with entry, current price, and sell levels |
| #trades | one "Trade Report #n" card per hour: what was bought (price, cost, est. profit, order expiry) and sold or settled (price, profit), plus total money; startup banners |
| #daily | a PNG (equity, daily P&L, Brier calibration) at `daily_report_time_local` |
| #alerts | orphan orders, position discrepancies, stale runner heartbeat, run failures, possible multi-outcome mispricings |
| #ask | talk to the bot; it runs the `query` task with read-only tools and, when you ask for a trade, shows a Confirm/Cancel button that runs the same guarded `place_order` path |

Slash commands: `/status`, `/kill [reason]` (halt + cancel open orders), `/resume`, `/arm` (live only), `/run_now`, `/cancel <order id prefix>`, `/mode`.

## Safety model

- `config.yaml` holds the tunable rules: the 50% invested target, 20% of total money per position, 5 open positions, the short-term tier (price 30 to 90 cents, 6 cents minimum edge, medium confidence, settles within 7 days), take-profit and stop-loss levels, cooldowns, and the Claude budget. `pm/risk.py` holds **absolute ceilings** that config cannot exceed (50% of total money per trade, 8 positions, price at most 0.97, 50 contracts, 12 orders/day).
- Every run recomputes invested share before each order and stops buying at the target; it never sells positions just to rebalance.
- Orders are written to the database as `pending_submit` **before** the API call; a deterministic idempotency key plus a unique index on (run, market, intent) make retries and crashes unable to double-place. A timeout leaves the order as `unknown` and blocks new orders until reconcile resolves it.
- Live mode requires `PM_MODE=live` **and** an explicit `/arm` (or `python -m scripts.halt arm`); a fresh live database starts disarmed.
- Kill switches: `/kill` in Discord, `python -m scripts.halt on`, or a file named `STOP` in the repo root.
- Automatic pauses: a daily loss stop at 25% of total money, a 6-hour cooldown after a settled loss, a halt after 5 losses in 7 days, the Thursday 2 to 6 AM ET maintenance window, and a one-hour Claude pause whenever a call reports a usage or rate limit.
- Claude is only called by the #ask assistant (`claude.max_calls_per_day`, default 60); the trading loop never calls it.

## Going live

Only after the paper run has produced enough settled assessments to judge calibration (the dashboard calibration tab compares the model Brier score against the market):

1. Set `PM_MODE=live` in `.env`, run `python -m scripts.init_db` (creates `live.db`, disarmed), then `python -m scripts.sync --check` (auth + balances).
2. Restart the services.
3. `/arm` in Discord. Expect to lose the bankroll; the point is the decision log.

Before the first live order, confirm via the automatic `orders.preview` step (the runner aborts on drift) that the price convention for NO orders (`ORDER_INTENT_BUY_SHORT`) is the NO price; the preview step rejects the order if the exchange interprets it differently.

## Layout

```
CLAUDE.md              rules for the headless #ask assistant
tasks/query.md         prompt + JSON schema for the #ask assistant
pm/                    client, broker (live + paper), execution, rules, manage, risk (fees, limits), scan (market data), settle, discord_reports, claude_runner, notify, report
runner/                hourly cycle and the poller
bot/                   discord.py bot
scripts/               CLIs; scripts/ro/* are the read-only ones the #ask assistant may run (funnel.py shows the market filter counts)
dashboard/app.py       Streamlit
database/schema.sql    schema (databases live in PM_DATA_DIR)
deploy/                systemd units, Ubuntu installer, Windows notes
tests/                 pytest suite
.claude/headless-settings.json   permissions + guard hook applied to the #ask assistant
```

## Troubleshooting

- `this workspace has not been trusted` in the logs: run `claude` once in the repo and accept the prompt.
- #ask replies fail with a usage-limit message: the assistant pauses itself for an hour automatically.
- `python: command not found` inside the #ask assistant on Ubuntu: install `python-is-python3`.
- Database locked: make sure only one runner and one bot run against the same `PM_DATA_DIR`, and that the folder is not being synced.
