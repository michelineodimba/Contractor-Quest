"""Interfaces the orchestrator depends on. Real and mock adapters both implement these."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Protocol

from ..models import Bid, Submission


class MarketError(RuntimeError):
    pass


@dataclass
class WalletQuote:
    ok: bool
    warnings: List[str] = field(default_factory=list)
    fee: Optional[Decimal] = None


@dataclass
class Execution:
    op_id: str
    status: str
    ok: bool
    raw: Optional[dict] = None


class Wallet(Protocol):
    def quote_withdraw(self, amount: Decimal, destination: str) -> WalletQuote: ...
    def withdraw(self, amount: Decimal, destination: str, idempotency_key: str) -> Execution: ...


class Market(Protocol):
    name: str
    def deposit_address(self) -> str: ...
    def wallet_balance(self) -> Decimal: ...
    def post_job(self, title: str, description: str, budget: Decimal, tags: List[str]) -> str: ...
    def list_bids(self, job_id: str) -> List[Bid]: ...
    def counter(self, job_id: str, bid_id: str, amount: Decimal, reasoning: str) -> Optional[Bid]: ...
    def award(self, job_id: str, bid_id: str) -> None: ...
    def get_submission(self, job_id: str) -> Optional[Submission]: ...
    def request_revision(self, job_id: str, feedback: str) -> None: ...
    def accept(self, job_id: str) -> None: ...
    def dispute(self, job_id: str, reason: str) -> None: ...
