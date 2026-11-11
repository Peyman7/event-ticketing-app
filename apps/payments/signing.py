"""Stripe-style webhook signatures: header `t=<unix>,v1=<hex hmac_sha256(secret, "t.body")>`.

The timestamp is signed too, and we reject old timestamps, so a captured request can't be
replayed later (dedupe by event id covers replays within the window).
"""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-Signature"


class SignatureError(Exception):
    pass


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(body: bytes, header: str, secret: str, tolerance_s: int, now: float | None = None):
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        ts, given = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError) as exc:
        raise SignatureError("malformed signature header") from exc
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance_s:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):  # constant-time
        raise SignatureError("signature mismatch")
