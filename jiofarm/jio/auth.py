"""JioFarm — Jio selfcare authentication helpers."""

from __future__ import annotations

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

JIO_SEND_OTP = "https://www.jio.com/api/jio-login-service/login/sendOtp"
JIO_VALIDATE_OTP = "https://www.jio.com/api/jio-login-service/login/validateOtp"


def _accepted_response(res: requests.Response) -> bool:
    """Reject HTTP/application errors and HTML; a 200 alone proves nothing.

    This is defensive response validation, not a claim that a live login or
    promotion is eligible. The authenticated flow still requires live testing.
    """
    if res.status_code != 200:
        return False
    try:
        data = res.json()
    except ValueError:
        return False
    return (isinstance(data, dict) and bool(data)
            and data.get("success") is not False
            and not any(data.get(key) for key in ("error", "errorCode", "errorMessage")))


def jio_send_otp(s: requests.Session, phone: str) -> bool:
    """POST to Jio sendOtp endpoint. Returns True on success."""
    res = s.post(
        JIO_SEND_OTP,
        json={
            "mobileNumber": phone,
            "loginFlowType": "MOBILE",
            "alternateNumber": "",
        },
        timeout=10,
        allow_redirects=False,
    )
    return _accepted_response(res)


def jio_validate_otp(s: requests.Session, otp: str) -> bool:
    """POST to Jio validateOtp endpoint. Returns True on success."""
    res = s.post(JIO_VALIDATE_OTP, json={"otp": otp}, timeout=10, allow_redirects=False)
    return _accepted_response(res)
