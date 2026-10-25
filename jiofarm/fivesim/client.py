"""JioFarm — 5SIM.net API client.

5SIM API v1 reference:
- Base: https://5sim.net/v1
- Auth: Bearer token in Authorization header
- Buy:  GET /user/buy/activation/{country}/{operator}/{product}
- SMS:  GET /user/check/{id}
- Cancel: GET /user/cancel/{id}
"""

from __future__ import annotations

import math
import threading
import time
from urllib.parse import urlencode

import requests


class FiveSimError(Exception):
    pass


class FiveSim:
    """Minimal client for the 5SIM.net API."""

    BASE = "https://5sim.net/v1"
    MIN_GAP = 1.0  # seconds between API calls

    def __init__(self, api_key: str, product: str = "jiomart"):
        self.key = api_key
        self.product = product
        self._lock = threading.Lock()
        self._last = 0.0

    # ----------------------------------------------------------------- low level
    def _get(self, path: str, retries: int = 3) -> dict | str:
        if path.startswith(("/user/buy/", "/user/cancel/", "/user/finish/")):
            retries = 1  # A lost response does not prove the mutation failed.
        url = f"{self.BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/json",
        }
        for attempt in range(1, retries + 1):
            with self._lock:
                wait = self.MIN_GAP - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                res = requests.get(url, headers=headers, timeout=20, allow_redirects=False)
                if res.status_code == 429 and attempt < retries:
                    time.sleep(attempt * 3)
                    continue
                if res.status_code != 200:
                    raise FiveSimError(f"5SIM HTTP {res.status_code}; request not replayed")
                ct = res.headers.get("content-type", "")
                if "json" in ct:
                    return res.json()
                return res.text.strip()
            except requests.RequestException as exc:
                if attempt == retries:
                    raise FiveSimError(f"5SIM transport failure ({type(exc).__name__}); reconcile orders before retrying") from None
                time.sleep(2)
        raise FiveSimError("5SIM request failed after retries.")

    # ---------------------------------------------------------------- high level
    def balance(self) -> float:
        """Return balance in the provider's account currency; do not assume USD."""
        data = self._get("/user/profile")
        if isinstance(data, dict) and "balance" in data:
            try:
                amount = float(data["balance"])
                if math.isfinite(amount):
                    return amount
            except (TypeError, ValueError):
                pass
        raise FiveSimError("5SIM balance response is missing a finite balance")

    def rent(self, max_price: float | None = None) -> tuple[str, str]:
        """Buy an activation (GrizzlySMS-compatible alias for buy)."""
        return self.buy(product=self.product, max_price=max_price)

    def buy(self, country: str = "india", operator: str = "any",
            product: str = "jiomart", max_price: float | None = None) -> tuple[str, str]:
        """Buy an activation. Returns (activation_id, phone_number)."""
        path = f"/user/buy/activation/{country}/{operator}/{product}"
        if max_price is not None:
            if not math.isfinite(max_price) or max_price < 0:
                raise ValueError("max_price must be finite and non-negative")
            if operator != "any":
                raise ValueError("5SIM maxPrice requires operator=any")
            path += "?" + urlencode({"maxPrice": max_price})
        data = self._get(path)
        if isinstance(data, dict) and data.get("id") and data.get("phone"):
            return str(data["id"]), str(data["phone"])
        raise FiveSimError("5SIM purchase was not confirmed; reconcile orders before retrying")

    def ready(self, act_id: str) -> None:
        """5SIM auto-activates — no-op."""

    def complete(self, act_id: str) -> None:
        """Confirm completion using the documented finish endpoint."""
        data = self._get(f"/user/finish/{act_id}")
        if not isinstance(data, dict) or data.get("status") != "FINISHED":
            raise FiveSimError("5SIM completion was not confirmed")

    def status(self, act_id: str) -> tuple[str, str | None]:
        """GrizzlySMS-compatible alias for check()."""
        raw_status, code = self.check(act_id)
        # Map 5SIM statuses to GrizzlySMS format
        MAP = {
            "PENDING": "WAIT",
            "RECEIVED": "OK",
            "TIMEOUT": "CANCEL",
            "CANCELED": "CANCEL",
            "BANNED": "CANCEL",
            "FINISHED": "OK",
        }
        if raw_status not in MAP:
            raise FiveSimError("5SIM returned an unknown activation status")
        return MAP[raw_status], code

    def check(self, act_id: str) -> tuple[str, str | None]:
        """Check for SMS. Returns (status, code_or_None).

        Status values: 'PENDING', 'RECEIVED', 'TIMEOUT', 'CANCELED', 'BANNED'
        """
        data = self._get(f"/user/check/{act_id}")
        if isinstance(data, dict):
            status = str(data.get("status", "PENDING"))
            sms_list = data.get("sms", [])
            code = None
            if sms_list and isinstance(sms_list, list):
                code = sms_list[0].get("code") if isinstance(sms_list[0], dict) else None
            return status, code
        raise FiveSimError("5SIM status response was not valid JSON data")

    def cancel(self, act_id: str) -> None:
        """Cancel an activation (refund)."""
        data = self._get(f"/user/cancel/{act_id}")
        if not isinstance(data, dict) or data.get("status") != "CANCELED":
            raise FiveSimError("5SIM cancellation was not confirmed")


def poll_sms(client: FiveSim, act_id: str, timeout: int = 120, interval: int = 5) -> str:
    """Poll 5SIM until the SMS arrives or the deadline expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, code = client.check(act_id)
        if status == "RECEIVED" and code:
            return code
        if status in ("CANCELED", "TIMEOUT", "BANNED"):
            raise FiveSimError(f"activation {status}")
        time.sleep(interval)
    raise FiveSimError(f"SMS timeout after {timeout}s")