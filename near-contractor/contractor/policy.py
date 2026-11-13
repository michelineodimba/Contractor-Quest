"""The only code path that can move money.

The planner, negotiator and any LLM never hold a Treasury. The orchestrator asks it to pay;
it checks the allowlist, currency, per-payment cap and the ledger reservation, runs a dry-run
preflight on the wallet, and only then sends, with a deterministic idempotency key.
This is a second layer inside the owner-signed Agent API policy, which stays authoritative.
"""
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, FrozenSet

from .adapters.base import Execution, Wallet
from .ledger import BudgetLedger
from .money import D, up, fmt


class PolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class SpendPolicy:
    allowed_recipients: FrozenSet[str]
    currency: str
    max_single_payment: Decimal


class Treasury:
    def __init__(self, wallet: Wallet, ledger: BudgetLedger, policy: SpendPolicy, run_id: str):
        self._wallet, self._ledger, self._policy, self._run_id = wallet, ledger, policy, run_id
        self._paid_per_part: Dict[str, Decimal] = {}
        self.payments = []

    @property
    def wallet(self):
        return self._wallet

    def describe(self) -> dict:
        p = self._policy
        return {"allowed_recipients": sorted(p.allowed_recipients), "currency": p.currency,
                "max_single_payment": str(p.max_single_payment), "budget": str(self._ledger.total),
                "payments": list(self.payments)}

    def pay(self, *, part_id: str, recipient: str, amount, currency: str, purpose: str) -> Execution:
        amount = up(amount)
        p = self._policy
        if amount <= 0:
            raise PolicyViolation("payment must be positive")
        if currency != p.currency:
            raise PolicyViolation(f"currency {currency} not allowed (only {p.currency})")
        if recipient not in p.allowed_recipients:
            raise PolicyViolation("recipient is not on the allowlist")
        if amount > p.max_single_payment:
            raise PolicyViolation(f"{fmt(amount, currency)} exceeds per-payment cap {fmt(p.max_single_payment, currency)}")
        held = self._ledger.reserved.get(part_id)
        if held is None:
            raise PolicyViolation(f"no budget reservation for part {part_id}")
        already = self._paid_per_part.get(part_id, Decimal(0))
        if already + amount > held:
            raise PolicyViolation(f"payment would exceed the reservation for part {part_id}")

        quote = self._wallet.quote_withdraw(amount, recipient)
        if not quote.ok or quote.warnings:
            raise PolicyViolation(f"wallet preflight refused: {'; '.join(quote.warnings) or 'not ok'}")

        key = hashlib.sha256(f"{self._run_id}|{part_id}|{recipient}|{amount}|{already}".encode()).hexdigest()[:32]
        result = self._wallet.withdraw(amount, recipient, key)
        if not result.ok:
            raise PolicyViolation(f"wallet execution did not succeed: status={result.status}")
        self._paid_per_part[part_id] = already + amount
        self.payments.append({"part_id": part_id, "amount": str(amount), "to": recipient,
                              "op_id": result.op_id, "status": result.status})
        return result
