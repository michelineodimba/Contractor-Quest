from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Optional, List, Dict, Any
import time

from .money import D


@dataclass(frozen=True)
class Bid:
    bid_id: str
    bidder: str
    amount: Decimal
    eta_seconds: int = 0
    proposal: str = ""
    reputation: Optional[float] = None


@dataclass
class Submission:
    content: str
    proof_sha256: Optional[str] = None


@dataclass
class CheckResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    method: str = "deterministic"


@dataclass
class Part:
    id: str
    title: str
    spec: str
    mode: str = "buy"                      # "make" (do it itself) or "buy"
    weight: Decimal = Decimal(1)           # relative share of the buy budget
    checks: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    cap: Decimal = Decimal(0)              # all-in spend cap; set by code, never by an LLM


@dataclass
class Task:
    title: str
    description: str
    budget: Decimal
    currency: str
    parts: List[Part] = field(default_factory=list)


@dataclass
class Plan:
    task_title: str
    budget: Decimal
    currency: str
    reserve: Decimal
    parts: List[Part]

    def total_caps(self) -> Decimal:
        return sum((p.cap for p in self.parts), Decimal(0))


@dataclass
class Receipt:
    run_id: str
    part_id: str
    title: str
    kind: str                              # "make" | "buy"
    status: str                            # paid | self_done | withheld | no_deal | unresolved | error
    seller: str
    currency: str
    quoted: Optional[str] = None
    final_price: str = "0"
    fee: str = "0"
    total_paid: str = "0"
    negotiation: List[dict] = field(default_factory=list)
    market_job_id: Optional[str] = None
    bid_id: Optional[str] = None
    payments: List[dict] = field(default_factory=list)
    check: Dict[str, Any] = field(default_factory=dict)
    deliverable_sha256: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)
