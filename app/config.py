"""Configuration. Everything is env-driven so the container stays stateless."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/secrets"))


def _secret(filename: str, env_key: str, default: str = "") -> str:
    """Env var wins; otherwise read the self-generated secret from the volume.

    Secrets are created inside the ntfy container on first start and shared
    over a read-only volume, so nothing is generated on the host.
    """
    value = os.getenv(env_key, "").strip()
    if value:
        return value
    try:
        return (SECRETS_DIR / filename).read_text().strip()
    except OSError:
        return default


def _env(key: str, default: str = "") -> str:
    """The variable's value, or default when it is unset OR blank.

    Compose passes a line like `SCAN_HOUR=` in .env through as an empty
    string. Treating that as "use the default" stops a half-edited .env from
    crashing the app on int("").
    """
    value = os.getenv(key, "").strip()
    return value if value else default


def _bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in {"1", "true", "yes", "on"}


def _list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


@dataclass
class Settings:
    # --- storage ---
    database_url: str = _env("DATABASE_URL", "sqlite:////data/moose.db")

    # --- scan schedule (see app/market.py) ---
    # By default the scan runs after every US close, this many minutes after
    # 16:00 New York time, whatever TZ is. TZ only changes how times are shown.
    scan_after_close_min: int = int(_env("SCAN_AFTER_CLOSE_MINUTES", "60"))
    # Advanced: a fixed local time (HH:MM, in TZ) on SCAN_DAYS instead.
    scan_time: str = _env("SCAN_TIME", "")
    scan_days: str = _env("SCAN_DAYS", "")
    # Older names for a fixed time, still honoured.
    scan_hour_raw: str = _env("SCAN_HOUR", "")
    scan_minute_raw: str = _env("SCAN_MINUTE", "")
    scan_on_startup: bool = _bool("SCAN_ON_STARTUP", True)

    # Commission per order, used to pre-fill the position form. Each trade
    # keeps whatever was actually entered, so changing this never rewrites
    # what you already recorded.
    default_commission: float = float(_env("DEFAULT_COMMISSION", "0"))

    # Opening balance for the Earnings tab's account curve. Left at 0, the
    # tab uses the most cash the recorded trades ever needed at once, so the
    # line starts at the capital they actually demanded.
    starting_cash: float = float(_env("STARTING_CASH", "0"))

    # --- history ---
    history_years: int = int(_env("HISTORY_YEARS", "5"))
    fetch_pause_sec: float = float(_env("FETCH_PAUSE_SEC", "1.2"))
    fetch_batch_size: int = int(_env("FETCH_BATCH_SIZE", "8"))
    fetch_retries: int = int(_env("FETCH_RETRIES", "3"))

    # --- indicator defaults (overridable per ticker in the UI) ---
    atr_len: int = int(_env("ATR_LEN", "10"))
    atr_mult: float = float(_env("ATR_MULT", "3.0"))
    sma_len: int = int(_env("SMA_LEN", "200"))
    di_len: int = int(_env("DI_LEN", "14"))
    adx_len: int = int(_env("ADX_LEN", "14"))
    adx_min: float = float(_env("ADX_MIN", "20"))
    # Filter applied to newly seeded tickers: none | regime | adx_rising | adx_only | full
    # Default is "none" (unfiltered). Across 39 tickers out-of-sample it had
    # negative expectancy on only 3, against 9-14 for every filtered variant,
    # and the best median total return. The Supertrend exit does the risk work.
    default_profile: str = _env("DEFAULT_PROFILE", "none").strip().lower()

    # --- trading costs applied in the backtest ---
    # Percent per side, covering commission plus expected slippage. 0.05 each
    # way is 0.1% a round trip, which is realistic for liquid US equities.
    cost_pct_per_side: float = float(_env("COST_PCT_PER_SIDE", "0.05"))

    # --- liquidity / suitability screen ---
    min_avg_volume: float = float(_env("MIN_AVG_VOLUME", "3000000"))
    min_atr_pct: float = float(_env("MIN_ATR_PCT", "1.5"))
    max_atr_pct: float = float(_env("MAX_ATR_PCT", "8.0"))

    # --- notifications ---
    notify_channels: list[str] = field(default_factory=lambda: _list("NOTIFY_CHANNELS", "ntfy"))
    notify_detail: str = _env("NOTIFY_DETAIL", "full").strip().lower()
    notify_heartbeat: bool = _bool("NOTIFY_HEARTBEAT", False)
    ntfy_url: str = _env("NTFY_URL", "https://ntfy.sh")
    ntfy_topic: str = _env("NTFY_TOPIC", "SupertrendMoose")
    ntfy_token: str = _env("NTFY_TOKEN", "")
    ntfy_user: str = _env("NTFY_USER", "")
    ntfy_password: str = _secret("ntfy-moose.pass", "NTFY_PASSWORD")
    telegram_token: str = _env("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID", "")
    discord_webhook: str = _env("DISCORD_WEBHOOK", "")
    smtp_host: str = _env("SMTP_HOST", "")
    smtp_port: int = int(_env("SMTP_PORT", "587"))
    smtp_user: str = _env("SMTP_USER", "")
    smtp_pass: str = _secret("smtp.pass", "SMTP_PASS")
    smtp_from: str = _env("SMTP_FROM", "")
    smtp_to: list[str] = field(default_factory=lambda: _list("SMTP_TO"))
    smtp_tls: bool = _bool("SMTP_TLS", True)

    # --- access ---
    auth_token: str = _secret("api-token", "AUTH_TOKEN")
    base_url: str = _env("BASE_URL", "")      # used in alert links


settings = Settings()
