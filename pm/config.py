from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env", override=False)


class FavoritesCfg(BaseModel):
    price_min: Decimal = Decimal("0.65")
    price_max: Decimal = Decimal("0.90")
    bias: Decimal = Decimal("0.025")
    max_spread: Decimal = Decimal("0.04")
    min_open_interest: Decimal = Decimal("300")
    oi_full: Decimal = Decimal("2000")
    min_hours: Decimal = Decimal("3")
    max_days: Decimal = Decimal("7")
    fetch_window_days: Decimal = Decimal("21")
    max_positions_per_category_pct: Decimal = Decimal("0.6")
    max_per_event: int = 1
    check_top_n: int = 80


class SizingCfg(BaseModel):
    kelly_switch_total: Decimal = Decimal("50")
    kelly_multiplier: Decimal = Decimal("0.5")


class CalibrationCfg(BaseModel):
    prior_weight: int = 50
    min_n_to_use: int = 30
    price_bands: list[Decimal] = Field(default_factory=lambda: [Decimal("0.65"), Decimal("0.75"), Decimal("0.85"), Decimal("0.91")])


class PortfolioCfg(BaseModel):
    invest_target_pct: Decimal = Decimal("0.50")
    max_position_pct_of_total: Decimal = Decimal("0.20")
    min_cash_usd: Decimal = Decimal("1.00")


class ExitsCfg(BaseModel):
    take_profit_cents: Decimal = Decimal("0.08")
    take_profit_price: Decimal = Decimal("0.95")
    stop_loss_cents: Decimal = Decimal("0.20")
    freeze_before_game_minutes: int = 60


class Liquidity(BaseModel):
    displayed_size_multiple: Decimal = Decimal("3")


class OrderDefaults(BaseModel):
    type: Literal["limit", "market"] = "limit"
    post_only: bool = True
    tif: Literal["GTD", "GTC", "IOC", "FOK"] = "GTD"
    gtd_hours: Decimal = Decimal("3")
    price_rule: Literal["best_bid_plus_tick", "mid", "best_ask"] = "best_bid_plus_tick"


class HaltAfterLosses(BaseModel):
    count: int = 5
    window_days: int = 7


class MaintenanceWindow(BaseModel):
    weekday: int = 3
    start: str = "02:00"
    end: str = "06:00"


class ClaudeCfg(BaseModel):
    model: str = "opus"
    max_calls_per_day: int = 60
    query_max_turns: int = 10
    timeout_seconds: int = 300
    quiet_hours_local: list[int] = Field(default_factory=list)


class ScanCfg(BaseModel):
    max_markets: int = 1500
    categories: list[str] = Field(default_factory=lambda: ["politics", "geopolitics", "macro", "science", "culture", "finance", "sports"])


class PaperCfg(BaseModel):
    starting_cash: Decimal = Decimal("10.00")
    maker_fill_fraction: Decimal = Decimal("0.5")
    max_slippage: Decimal = Decimal("0.02")


class Config(BaseModel):
    run_interval_hours: Decimal = Decimal("1")
    poll_seconds: int = 60
    daily_report_time_local: str = "00:05"
    display_timezone: str = "America/New_York"
    max_new_orders_per_run: int = 5
    max_new_orders_per_day: int = 10
    max_open_positions: int = 5
    never_buy_above: Decimal = Decimal("0.97")
    portfolio: PortfolioCfg = PortfolioCfg()
    favorites: FavoritesCfg = FavoritesCfg()
    sizing: SizingCfg = SizingCfg()
    calibration: CalibrationCfg = CalibrationCfg()
    exits: ExitsCfg = ExitsCfg()
    liquidity: Liquidity = Liquidity()
    order_defaults: OrderDefaults = OrderDefaults()
    daily_loss_stop_pct: Decimal = Decimal("0.25")
    loss_cooldown_hours: Decimal = Decimal("6")
    halt_after_losses: HaltAfterLosses = HaltAfterLosses()
    no_trade_categories: list[str] = Field(default_factory=lambda: ["crypto"])
    no_trade_market_types: list[str] = Field(default_factory=list)
    maintenance_window_et: MaintenanceWindow = MaintenanceWindow()
    claude: ClaudeCfg = ClaudeCfg()
    scan: ScanCfg = ScanCfg()
    paper: PaperCfg = PaperCfg()


@dataclass(frozen=True)
class Settings:
    mode: str
    dry_run: bool
    data_dir: Path
    key_id: str | None
    secret_key: str | None
    discord_token: str | None
    discord_guild_id: int | None
    discord_owner_id: int | None
    discord_ask_channel_id: int | None
    webhook_runs: str | None
    webhook_trades: str | None
    webhook_daily: str | None
    webhook_alerts: str | None
    healthcheck_url: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / f"{self.mode}.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _int_or_none(v: str | None) -> int | None:
    v = (v or "").strip()
    return int(v) if v else None


def _str_or_none(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def load_settings() -> Settings:
    mode = (os.environ.get("PM_MODE") or "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ValueError(f"PM_MODE must be paper or live, got {mode!r}")
    data_dir = Path(os.environ.get("PM_DATA_DIR") or (REPO_ROOT / "data")).expanduser().resolve()
    for sub in ("logs", "runs", "locks", "backups"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    return Settings(
        mode=mode,
        dry_run=(os.environ.get("PM_DRY_RUN") or "0").strip().lower() in ("1", "true", "yes"),
        data_dir=data_dir,
        key_id=_str_or_none(os.environ.get("POLYMARKET_KEY_ID")),
        secret_key=_str_or_none(os.environ.get("POLYMARKET_SECRET_KEY")),
        discord_token=_str_or_none(os.environ.get("DISCORD_BOT_TOKEN")),
        discord_guild_id=_int_or_none(os.environ.get("DISCORD_GUILD_ID")),
        discord_owner_id=_int_or_none(os.environ.get("DISCORD_OWNER_ID")),
        discord_ask_channel_id=_int_or_none(os.environ.get("DISCORD_ASK_CHANNEL_ID")),
        webhook_runs=_str_or_none(os.environ.get("DISCORD_WEBHOOK_RUNS")),
        webhook_trades=_str_or_none(os.environ.get("DISCORD_WEBHOOK_TRADES")),
        webhook_daily=_str_or_none(os.environ.get("DISCORD_WEBHOOK_DAILY")),
        webhook_alerts=_str_or_none(os.environ.get("DISCORD_WEBHOOK_ALERTS")),
        healthcheck_url=_str_or_none(os.environ.get("HEALTHCHECK_URL")),
    )


def load_config(path: Path | None = None) -> tuple[Config, str]:
    p = path or (REPO_ROOT / "config.yaml")
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    cfg = Config.model_validate(data)
    return cfg, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
