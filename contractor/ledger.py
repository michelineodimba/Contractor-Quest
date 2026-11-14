"""Budget ledger. The hard ceiling: reserved + spent can never exceed the total."""
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from .money import D, up, fmt


class BudgetError(RuntimeError):
    pass


@dataclass
class BudgetLedger:
    total: Decimal
    currency: str
    log_path: Optional[Path] = None
    reserved: Dict[str, Decimal] = field(default_factory=dict)
    spent: Dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self):
        self.total = D(self.total)
        if self.total <= 0:
            raise BudgetError("budget must be positive")

    @property
    def total_reserved(self) -> Decimal:
        return sum(self.reserved.values(), Decimal(0))

    @property
    def total_spent(self) -> Decimal:
        return sum(self.spent.values(), Decimal(0))

    def available(self) -> Decimal:
        return self.total - self.total_reserved - self.total_spent

    def reserve(self, key: str, amount) -> Decimal:
        amount = up(amount)
        if amount <= 0:
            raise BudgetError("reservation must be positive")
        if key in self.reserved or key in self.spent:
            raise BudgetError(f"{key} already has a reservation or spend")
        if amount > self.available():
            raise BudgetError(
                f"cannot reserve {fmt(amount, self.currency)} for {key}: "
                f"only {fmt(self.available(), self.currency)} available")
        self.reserved[key] = amount
        self._log("reserve", key, amount)
        return amount

    def commit(self, key: str, amount) -> Decimal:
        amount = up(amount)
        held = self.reserved.get(key)
        if held is None:
            raise BudgetError(f"no reservation for {key}")
        if amount > held:
            raise BudgetError(f"commit {fmt(amount)} exceeds reservation {fmt(held)} for {key}")
        del self.reserved[key]
        self.spent[key] = amount
        self._log("commit", key, amount, released=held - amount)
        return amount

    def release(self, key: str) -> Decimal:
        held = self.reserved.pop(key, Decimal(0))
        if held:
            self._log("release", key, held)
        return held

    def _log(self, event, key, amount, **extra):
        assert self.total_reserved + self.total_spent <= self.total, "ledger invariant broken"
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {"event": event, "key": key, "amount": str(amount),
                   "available": str(self.available()), **{k: str(v) for k, v in extra.items()}}
            with open(self.log_path, "a") as f:
                f.write(json.dumps(row) + "\n")
