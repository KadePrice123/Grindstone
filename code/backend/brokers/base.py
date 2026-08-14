"""The broker contract every adapter implements (REQUIREMENTS.md 6.10).

M1 scope: identity + connection test + declared capabilities. Positions,
orders, chains, and streams are added milestone by milestone — the UI reads
`capabilities` and disables (with a reason) whatever an adapter can't do.
Adapters never leak broker-shaped payloads upward; they return plain dicts
from pure parse functions so the gate can test them offline.
"""
from __future__ import annotations

from typing import Any

BROKERS = ("alpaca", "tastytrade", "webull", "fidelity")

# Credential fields each broker needs; `hint_last4` names the field whose last
# four characters are stored in plaintext for display (never the secret itself).
CREDENTIAL_FIELDS: dict[str, dict[str, Any]] = {
    "alpaca": {
        "fields": ["key_id", "secret_key"],
        "hint_last4": "key_id",
    },
    "tastytrade": {
        # No hint: both fields ARE secrets, and the hint column is plaintext in
        # a cloud-synced DB. Alpaca's key_id is a public identifier, so it may
        # carry one; a refresh token may not.
        "fields": ["client_secret", "refresh_token"],
        "hint_last4": None,
    },
    "webull": {"fields": ["app_key", "app_secret"], "hint_last4": "app_key"},
    "fidelity": {"fields": [], "hint_last4": None},  # read-only tier, no direct keys
}


class BrokerError(Exception):
    """Normalized adapter failure with a user-presentable message."""


class BrokerAdapter:
    name: str = "base"
    capabilities: dict[str, Any] = {}

    def test_connection(self) -> dict[str, Any]:
        """Cheap, read-only liveness+auth check. Returns normalized summary."""
        raise BrokerError(f"{self.name}: not implemented yet")


def not_supported(broker: str) -> dict[str, Any]:
    reasons = {
        "webull": "Webull needs an approved OpenAPI application first (REQUIREMENTS.md 7.3).",
        "fidelity": "Fidelity has no retail trading API; read-only import lands later (7.4).",
    }
    return {"ok": False, "error": reasons.get(broker, f"unknown broker {broker!r}")}
