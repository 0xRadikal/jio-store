"""Jio subscriber checks; distinguish ineligibility from service failures."""

from __future__ import annotations

import requests

from jiofarm.jio.auth import UA

JIO_CHECK = "https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{phone}"


class SubscriberCheckError(RuntimeError):
    """The service could not provide a trustworthy subscriber check."""


def jio_check_subscriber(phone: str) -> bool:
    """Return False only for the observed NOT_SUBSCRIBED_USER response.

    Timeouts, access restrictions, malformed responses and service failures are
    operational errors, not evidence that the phone is not a Jio subscriber.
    A positive check does not establish promotional eligibility.
    """
    try:
        res = requests.get(
            JIO_CHECK.format(phone=phone),
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Referer": "https://www.jio.com/",
            },
            timeout=10,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise SubscriberCheckError(f"Subscriber check transport failure ({type(exc).__name__})") from None
    try:
        data = res.json()
    except ValueError:
        raise SubscriberCheckError(f"Subscriber check returned non-JSON data (HTTP {res.status_code})") from None
    if not isinstance(data, dict):
        raise SubscriberCheckError("Subscriber check returned an unexpected response structure")
    if res.status_code == 400 and data.get("errorMessage") == "NOT_SUBSCRIBED_USER":
        return False
    if res.status_code != 200:
        raise SubscriberCheckError(f"Subscriber check failed (HTTP {res.status_code})")
    if data.get("success") is False or data.get("error") or data.get("errorMessage"):
        raise SubscriberCheckError("Subscriber check returned an application error")
    return True


def normalize_phone(raw: str) -> str:
    """Strip non-digits and return the last 10 digits."""
    digits = "".join(c for c in raw if c.isdigit())
    return digits[-10:] if len(digits) > 10 else digits
