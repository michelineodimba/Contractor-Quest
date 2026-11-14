"""Adapter for the NEAR Intents Agent API: a policy-bound wallet whose rules the owner signed.

Confirmed from the docs: headers X-API-Key (partner key) and X-Grant-Token, an Idempotency-Key on every
money action except `dry` quotes, POST /v1/agents/{agent_id}/withdraw with `dry: true` for a preview that
returns warnings instead of refusing, status via GET /v1/status?correlation_id=op_...&wait_ms=...
Marked VERIFY: the exact body field names, units, and the success status string.
Fail closed: an unknown terminal status is treated as not ok.
"""
from decimal import Decimal
from typing import Optional

from ..http import Http, HttpError
from ..money import D
from .base import Execution, WalletQuote

SUCCESS = {"SUCCESS", "COMPLETED", "SETTLED", "EXECUTED"}      # VERIFY
FAILED = {"FAILED", "CANCELLED", "REJECTED", "REFUNDED"}       # VERIFY


def to_atomic(amount: Decimal, decimals: int) -> str:
    return str(int((D(amount) * (10 ** decimals)).to_integral_value()))


class AgentApiWallet:
    def __init__(self, base_url: str, api_key: str, grant_token: str, agent_id: str,
                 asset_id: str, decimals: int, http: Optional[Http] = None, wait_ms: int = 30000):
        self.http = http or Http(base_url, {"X-API-Key": api_key, "X-Grant-Token": grant_token})
        self.agent_id, self.asset_id, self.decimals, self.wait_ms = agent_id, asset_id, decimals, wait_ms

    def _body(self, amount, destination, dry: bool) -> dict:
        return {"asset_id": self.asset_id, "amount": to_atomic(amount, self.decimals),   # VERIFY names and units
                "destination": destination, **({"dry": True} if dry else {})}

    def quote_withdraw(self, amount, destination) -> WalletQuote:
        try:
            r = self.http.request("POST", f"/v1/agents/{self.agent_id}/withdraw",
                                  json_body=self._body(amount, destination, dry=True))
        except HttpError as e:
            return WalletQuote(False, [f"quote refused: HTTP {e.status}"])
        warnings = [str(w.get("code", w) if isinstance(w, dict) else w) for w in (r or {}).get("warnings", [])]
        return WalletQuote(True, warnings)

    def withdraw(self, amount, destination, idempotency_key) -> Execution:
        r = self.http.request("POST", f"/v1/agents/{self.agent_id}/withdraw",
                              json_body=self._body(amount, destination, dry=False),
                              headers={"Idempotency-Key": idempotency_key})
        op = (r or {}).get("correlation_id", "")
        st = self.http.request("GET", "/v1/status", params={"correlation_id": op, "wait_ms": self.wait_ms})
        status = str((st or {}).get("status", "UNKNOWN")).upper()
        return Execution(op, status, status in SUCCESS, st)
