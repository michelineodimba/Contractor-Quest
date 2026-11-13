"""Negotiation: accept anything at or under the ceiling; otherwise counter exactly once; then accept or walk away.

The ceiling is a hard limit. The negotiator can never accept a quote above it, whatever the reply says.
Only the per-part ceiling is revealed to sellers, never the total budget.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from .adapters.base import Market
from .models import Bid
from .money import D, fmt


@dataclass
class NegotiationResult:
    accepted: Optional[Bid]
    steps: List[dict] = field(default_factory=list)


def rank(bids: List[Bid]) -> List[Bid]:
    """Cheapest first, then faster, then better reputation."""
    return sorted(bids, key=lambda b: (b.amount, b.eta_seconds or 10**9, -(b.reputation or 0)))


def counter_reasoning(part_title: str, quote: Bid, ceiling: Decimal, currency: str) -> str:
    over = (quote.amount - ceiling) / ceiling * 100
    return (f"Thanks for bidding on '{part_title}'. The most I can award for this scope is "
            f"{fmt(ceiling, currency)} (your quote of {fmt(quote.amount, currency)} is {over:.0f}% above that). "
            f"This is a fixed allocation for this part, not an opening position. "
            f"If you can deliver the stated acceptance criteria at {fmt(ceiling, currency)}, "
            f"I will award immediately and fund escrow on award. This is my single counter-offer.")


def negotiate(market: Market, job_id: str, bids: List[Bid], ceiling: Decimal,
              part_title: str, currency: str) -> NegotiationResult:
    ceiling = D(ceiling)
    steps: List[dict] = []
    if not bids:
        steps.append({"action": "walk_away", "reason": "no bids received"})
        return NegotiationResult(None, steps)

    ranked = rank(bids)
    within = [b for b in ranked if b.amount <= ceiling]
    if within:
        best = within[0]
        steps.append({"action": "accept", "bidder": best.bidder, "amount": str(best.amount),
                      "reason": "quote within planned spend"})
        return NegotiationResult(best, steps)

    lowest = ranked[0]
    reasoning = counter_reasoning(part_title, lowest, ceiling, currency)
    steps.append({"action": "counter", "bidder": lowest.bidder, "quoted": str(lowest.amount),
                  "offered": str(ceiling), "reasoning": reasoning})
    revised = market.counter(job_id, lowest.bid_id, ceiling, reasoning)

    if revised is not None and revised.amount <= ceiling:
        steps.append({"action": "accept", "bidder": revised.bidder, "amount": str(revised.amount),
                      "reason": "seller met the counter"})
        return NegotiationResult(revised, steps)

    steps.append({"action": "walk_away",
                  "reason": "no revised quote" if revised is None
                  else f"revised quote {revised.amount} still above ceiling {ceiling}"})
    return NegotiationResult(None, steps)
