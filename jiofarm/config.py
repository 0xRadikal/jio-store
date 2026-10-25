"""JioFarm — centralised configuration from .env and CLI args."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _find_env() -> Path:
    """Look for .env next to the project root, falling back to cwd."""
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        candidate = base / ".env"
        if candidate.exists():
            return candidate
    return Path(".env")


def load_config() -> "Config":
    """Load .env and return a populated Config instance."""
    env_path = _find_env()
    load_dotenv(env_path)

    provider = os.getenv("PROVIDER", "grizzlysms").strip().lower()

    if provider == "grizzlysms":
        api_key = os.getenv("GRIZZLY_API_KEY", "").strip()
        if not api_key or api_key == "your_api_key_here":
            msg = (
                "GRIZZLY_API_KEY belum diset di .env\n"
                "  1. Buka https://grizzlysms.com — daftar & topup\n"
                "  2. Copy API key dari dashboard\n"
                "  3. Paste ke .env: GRIZZLY_API_KEY=xxxx"
            )
            raise SystemExit(msg)
        product = os.getenv("GRIZZLY_PRODUCT", "jio").strip()
    elif provider == "fivesim":
        api_key = os.getenv("FIVESIM_API_KEY", "").strip()
        if not api_key or api_key == "your_api_key_here":
            msg = (
                "FIVESIM_API_KEY belum diset di .env\n"
                "  1. Buka https://5sim.net — daftar & topup\n"
                "  2. Copy API key (JWT token) dari dashboard\n"
                "  3. Paste ke .env: FIVESIM_API_KEY=xxxx"
            )
            raise SystemExit(msg)
        product = os.getenv("FIVESIM_PRODUCT", "jiomart").strip()
    else:
        raise SystemExit(f"PROVIDER tidak dikenal: '{provider}'. Pilih: grizzlysms, fivesim")

    config = Config(
        provider=provider,
        api_key=api_key,
        product=product,
        max_price=float(os.getenv("MAX_PRICE", "1.0")),
        cancel_delay=float(os.getenv("CANCEL_DELAY_SECONDS", "150")),
        otp_fail_delay=float(os.getenv("OTP_FAIL_DELAY_SECONDS", "420")),
        concurrency=int(os.getenv("CONCURRENCY", "2")),
        db_path=os.getenv("DB_PATH", "results.db"),
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        tg_chat_id=os.getenv("TG_CHAT_ID", "").strip(),
    )
    config.validate()
    return config


@dataclass
class Config:
    """All runtime configuration, sourced from .env and overridden by CLI args."""

    provider: str = "grizzlysms"
    api_key: str = field(default="", repr=False)
    product: str = "jio"  # GrizzlySMS: "jio", 5SIM: "jiomart"
    max_price: float = 1.0
    cancel_delay: float = 150.0
    otp_fail_delay: float = 420.0
    concurrency: int = 2
    db_path: str = "results.db"

    # telegram (optional)
    tg_bot_token: str = field(default="", repr=False)
    tg_chat_id: str = field(default="", repr=False)

    # runtime overrides (set by CLI)
    max_price_override: float | None = field(default=None, repr=False)
    target: int | None = field(default=None, repr=False)
    duration: float | None = field(default=None, repr=False)
    count: int = field(default=1, repr=False)
    concurrency_override: int | None = field(default=None, repr=False)

    @property
    def effective_max_price(self) -> float | None:
        if self.max_price_override is not None:
            return self.max_price_override
        return self.max_price

    @property
    def effective_concurrency(self) -> int:
        return self.concurrency if self.concurrency_override is None else self.concurrency_override

    def validate(self) -> None:
        """Validate before any provider request or worker is started."""
        for name, value in (("concurrency", self.effective_concurrency), ("count", self.count)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in (("max_price", self.effective_max_price),
                            ("cancel_delay", self.cancel_delay),
                            ("otp_fail_delay", self.otp_fail_delay)):
            if value is None or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.target is not None and (not isinstance(self.target, int) or self.target < 1):
            raise ValueError("target must be a positive integer")
        if self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0):
            raise ValueError("duration must be positive and finite")

    def redact(self, message: str) -> str:
        """Remove configured credentials from diagnostic text."""
        for secret in (self.api_key, self.tg_bot_token):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)