"""Polymarket auth scaffolding for the (disabled-by-default) execution path.

L2 (HMAC API credentials) per the official client implementation
(github.com/Polymarket/py-clob-client, headers POLY_*; retrieved 2026-06-11):
sign `timestamp + METHOD + path + body` with HMAC-SHA256 over the urlsafe-base64
decoded secret, urlsafe-base64 encode the digest.

L1 (EIP-712 wallet signing) requires an Ethereum signing stack we deliberately do
not ship in the discovery-only default — the stub fails closed until execution
mode is implemented end-to-end behind the geoblock gate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class L2Credentials:
    address: str
    api_key: str
    api_secret: str
    api_passphrase: str


def l2_signature(secret_b64url: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    key = base64.urlsafe_b64decode(secret_b64url)
    message = f"{timestamp}{method.upper()}{path}{body}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode()


def l2_headers(
    creds: L2Credentials, method: str, path: str, body: str = "", timestamp: int | None = None
) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "POLY_ADDRESS": creds.address,
        "POLY_SIGNATURE": l2_signature(creds.api_secret, ts, method, path, body),
        "POLY_TIMESTAMP": ts,
        "POLY_API_KEY": creds.api_key,
        "POLY_PASSPHRASE": creds.api_passphrase,
    }


def l1_sign_order(*args: object, **kwargs: object) -> None:
    """EIP-712 order signing — intentionally not implemented.

    Execution is disabled by default (SPEC prime directive 5); implementing this
    requires an audited Ethereum signing dependency and a passing geoblock check.
    """
    raise NotImplementedError(
        "L1 EIP-712 signing is not shipped; execution path is disabled by design"
    )
