"""JioFarm — GrizzlySMS API client."""

from __future__ import annotations

import math
import threading
import time

import requests


class GrizzlyError(Exception):
    pass


class GrizzlySMS:
    """Minimal client for the GrizzlySMS stubs/handler_api.php endpoint."""

    BASE = "https://api.grizzlysms.com/stubs/handler_api.php"
    MIN_GAP = 1.2  # seconds between API calls from this client

    def __init__(self, api_key: str, product: str = "jio"):
        self.key = api_key
        self.product = product
        self._lock = threading.Lock()
        self._last = 0.0

    # ----------------------------------------------------------------- low level
    def _get(self, params: dict, retries: int = 4) -> str:
        # A lost purchase response may still have charged the account.
        # Never automatically replay mutations, including after HTTP 429.
        if params.get("action") not in {"getBalance", "getStatus"}:
            retries = 1
        params = {**params, "api_key": self.key}
        for attempt in range(1, retries + 1):
            with self._lock:
                wait = self.MIN_GAP - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                res = requests.get(self.BASE, params=params, timeout=20, allow_redirects=False)
                if res.status_code == 429 and attempt < retries:
                    time.sleep(attempt * 3)
                    continue
                if res.status_code != 200:
                    raise GrizzlyError(f"GrizzlySMS HTTP {res.status_code}; request not replayed")
                return res.text.strip()
            except requests.RequestException as exc:
                if attempt == retries:
                    raise GrizzlyError(f"GrizzlySMS transport failure ({type(exc).__name__}); reconcile orders before retrying") from None
                time.sleep(2)
        raise GrizzlyError("GrizzlySMS request failed after retries.")

    @staticmethod
    def _response_error(operation: str, body: str) -> GrizzlyError:
        known = {"BAD_KEY", "NO_BALANCE", "NO_NUMBERS", "BAD_SERVICE", "BAD_STATUS",
                 "BAD_ACTION", "NO_ACTIVATION", "EARLY_CANCEL_DENIED", "ERROR_SQL",
                 "SERVICE_UNAVAILABLE_REGION"}
        code = body if body in known else "unexpected response"
        return GrizzlyError(f"GrizzlySMS {operation}: {code}")

    # ---------------------------------------------------------------- high level
    def balance(self) -> float:
        out = self._get({"action": "getBalance"})
        if out.startswith("ACCESS_BALANCE:"):
            try:
                amount = float(out.split(":", 1)[1])
                if math.isfinite(amount):
                    return amount
            except ValueError:
                pass
        raise self._response_error("balance", out)

    def rent(self, max_price: float | None = None) -> tuple[str, str]:
        p: dict = {"action": "getNumber", "service": self.product, "country": 22}
        if max_price is not None:
            if not math.isfinite(max_price) or max_price < 0:
                raise ValueError("max_price must be finite and non-negative")
            p["maxPrice"] = max_price
        out = self._get(p)
        if out.startswith("ACCESS_NUMBER:"):
            parts = out.split(":", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return parts[1], parts[2]
        raise self._response_error("rent", out)

    def status(self, act_id: str) -> tuple[str, str | None]:
        out = self._get({"action": "getStatus", "id": act_id})
        if out == "STATUS_WAIT_CODE":
            return "WAIT", None
        if out.startswith("STATUS_OK:"):
            return "OK", out.split(":", 1)[1]
        if out == "STATUS_CANCEL":
            return "CANCEL", None
        raise self._response_error("status", out)

    def ready(self, act_id: str) -> None:
        out = self._get({"action": "setStatus", "id": act_id, "status": 1})
        if out != "ACCESS_READY":
            raise self._response_error("ready", out)

    def complete(self, act_id: str) -> None:
        out = self._get({"action": "setStatus", "id": act_id, "status": 6})
        if out != "ACCESS_ACTIVATION":
            raise self._response_error("complete", out)

    def cancel(self, act_id: str) -> None:
        out = self._get({"action": "setStatus", "id": act_id, "status": 8})
        if out != "ACCESS_CANCEL":
            raise self._response_error("cancel", out)


def poll_otp(sms: GrizzlySMS, act_id: str, timeout: int = 120, interval: int = 5) -> str:
    """Poll GrizzlySMS until the OTP arrives or the deadline expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state, code = sms.status(act_id)
        if state == "OK" and code:
            return code
        if state == "CANCEL":
            raise GrizzlyError("activation cancelled upstream")
        time.sleep(interval)
    raise GrizzlyError(f"OTP timeout after {timeout}s")