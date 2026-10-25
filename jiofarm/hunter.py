"""JioFarm — Core hunt cycle orchestrator.

Ties together SMS provider rentals, Jio authentication, link hunting, and
storage.  Designed to be driven by the CLI layer (``cli.py``) so that the
live-dashboard can observe worker state in real time.

Supports multiple providers: GrizzlySMS, 5SIM.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

import requests

from jiofarm.config import Config
from jiofarm.console import tg_send
from jiofarm.grizzly.refund import RefundWorker
from jiofarm.jio.auth import UA, jio_send_otp, jio_validate_otp
from jiofarm.jio.check import jio_check_subscriber, normalize_phone
from jiofarm.jio.hunt import hunt_link
from jiofarm.storage.store import Store


# ------------------------------------------------------------------- provider protocol


class SMSProvider(Protocol):
    """Protocol that every SMS provider client must implement."""

    def balance(self) -> float: ...
    def rent(self, max_price: float | None = None) -> tuple[str, str]: ...
    def ready(self, act_id: str) -> None: ...
    def complete(self, act_id: str) -> None: ...
    def cancel(self, act_id: str) -> None: ...


def poll_otp(provider: SMSProvider, act_id: str, timeout: int = 120, interval: int = 5) -> str:
    """Poll the provider until the OTP arrives or the deadline expires.

    Uses duck-typing: tries ``status()`` first (GrizzlySMS), then ``check()`` (5SIM).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        # GrizzlySMS-style: .status() returns ("WAIT"/"OK"/"CANCEL", code)
        if hasattr(provider, "status"):
            state, code = provider.status(act_id)  # type: ignore[attr-defined]
            if state == "OK" and code:
                return code
            if state == "CANCEL":
                raise Exception("activation cancelled upstream")
        # 5SIM-style: .check() returns ("RECEIVED"/"PENDING"/..., code)
        elif hasattr(provider, "check"):
            status, code = provider.check(act_id)  # type: ignore[attr-defined]
            if status == "RECEIVED" and code:
                return code
            if status in ("CANCELED", "TIMEOUT", "BANNED"):
                raise Exception(f"activation {status}")
        else:
            raise Exception("provider tidak mendukung polling OTP")
        time.sleep(interval)
    raise Exception(f"OTP timeout setelah {timeout}s")


# ------------------------------------------------------------------- worker state


class Phase(Enum):
    IDLE = auto()
    RENTING = auto()
    CHECKING = auto()
    SENDING_OTP = auto()
    WAITING_OTP = auto()
    VALIDATING = auto()
    HUNTING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class WorkerState:
    wid: int
    phase: Phase = Phase.IDLE
    phone: str = ""
    act_id: str = ""
    otp: str = ""
    logged_in: bool = False
    link: str | None = None
    error: str = ""
    attempts: int = 0


# ------------------------------------------------------------------- hunt cycle


def hunt_once(
    provider: SMSProvider,
    store: Store,
    refunds: RefundWorker,
    slots: threading.Semaphore,
    cfg: Config,
    state: WorkerState,
) -> str | None:
    """Run one complete hunt attempt, updating *state* for live display.

    Returns the link if found, otherwise None.
    """
    slots.acquire()
    state.phase = Phase.RENTING
    state.phone = ""
    state.act_id = ""
    state.otp = ""
    state.logged_in = False
    state.link = None
    state.error = ""
    s: requests.Session | None = None

    try:
        # 1) rent a number --------------------------------------------------
        state.act_id, raw = provider.rent(cfg.effective_max_price)
        state.phone = normalize_phone(raw)

        # 2) check subscriber -----------------------------------------------
        state.phase = Phase.CHECKING
        if not jio_check_subscriber(state.phone):
            state.phase = Phase.ERROR
            state.error = "bukan pelanggan Jio"
            refunds.schedule(state.act_id, 120, aggressive=True)
            return None

        # 3) create Jio session --------------------------------------------
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.jio.com",
                "Referer": "https://www.jio.com/selfcare/login/",
            }
        )

        # 4) send OTP -------------------------------------------------------
        state.phase = Phase.SENDING_OTP
        if not jio_send_otp(s, state.phone):
            state.phase = Phase.ERROR
            state.error = "OTP gagal dikirim"
            refunds.schedule(state.act_id, cfg.otp_fail_delay)
            return None

        provider.ready(state.act_id)

        # 5) poll OTP -------------------------------------------------------
        state.phase = Phase.WAITING_OTP
        state.otp = poll_otp(provider, state.act_id)

        # 6) validate OTP ---------------------------------------------------
        state.phase = Phase.VALIDATING
        if not jio_validate_otp(s, state.otp):
            state.phase = Phase.ERROR
            state.error = "OTP invalid"
            refunds.schedule(state.act_id, cfg.cancel_delay)
            return None

        state.logged_in = True

        # 7) hunt link ------------------------------------------------------
        state.phase = Phase.HUNTING
        link = hunt_link(s)
        provider.complete(state.act_id)

        if link:
            state.link = link
            state.phase = Phase.DONE
            tg_send(
                f"🎯 Google AI Pro link!\n\nNomor : {state.phone}\nLink :\n{link}",
                cfg.tg_bot_token,
                cfg.tg_chat_id,
            )
        else:
            state.phase = Phase.DONE
            state.error = "login sukses tapi tidak ada promo Google aktif"

        return link

    except KeyboardInterrupt:
        raise
    except Exception as e:
        state.phase = Phase.ERROR
        state.error = cfg.redact(str(e))
        if state.act_id and not state.logged_in:
            refunds.schedule(state.act_id, cfg.cancel_delay)
        return None
    finally:
        try:
            if s is not None:
                s.close()
            store.save(state.phone, state.act_id, state.otp, state.logged_in, state.link)
        finally:
            state.attempts += 1
            slots.release()


# ------------------------------------------------------------------- provider factory


def create_provider(cfg: Config) -> SMSProvider:
    """Create the appropriate SMS provider client based on Config."""
    if cfg.provider == "grizzlysms":
        from jiofarm.grizzly.client import GrizzlySMS

        return GrizzlySMS(cfg.api_key, product=cfg.product)  # type: ignore[return-value]
    elif cfg.provider == "fivesim":
        from jiofarm.fivesim.client import FiveSim

        return FiveSim(cfg.api_key, product=cfg.product)  # type: ignore[return-value]
    else:
        raise SystemExit(f"Provider tidak dikenal: {cfg.provider}")