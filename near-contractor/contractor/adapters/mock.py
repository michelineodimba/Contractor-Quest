"""Offline simulation of the market and wallet, so the whole loop runs with no network and no funds.

This is a SIMULATION used for tests and the demo. It is not the real market.
"""
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from ..checker import sha256_text
from ..models import Bid, Submission
from ..money import D, down, up
from .base import Execution, MarketError, WalletQuote


@dataclass
class MockWorker:
    name: str
    price_factor: Decimal      # first quote as a multiple of the job budget
    floor_factor: Decimal      # lowest multiple of the job budget they accept after a counter
    quality: str = "good"      # good | bad | badhash | revises (fails first, passes after one revision)
    eta_seconds: int = 3600


class MockMarket:
    name = "mock-market"

    def __init__(self, scenarios: Dict[str, List[MockWorker]], fee_rate=Decimal("0.02"),
                 deposit_addr="mock-market.near"):
        self.scenarios, self.fee_rate, self._addr = scenarios, D(fee_rate), deposit_addr
        self.balance = Decimal(0)
        self.jobs: Dict[str, dict] = {}
        self.counters: List[dict] = []
        self._n = 0

    # wallet side
    def deposit_address(self): return self._addr
    def wallet_balance(self): return self.balance
    def credit(self, amount): self.balance += D(amount)

    # jobs
    def post_job(self, title, description, budget, tags):
        self._n += 1
        job_id = f"job-{self._n}"
        part = next((t.split(":", 1)[1] for t in tags if t.startswith("part:")), "")
        workers = self.scenarios.get(part, [])
        bids = {f"{job_id}-b{i}": (w, Bid(f"{job_id}-b{i}", w.name, up(D(budget) * w.price_factor), w.eta_seconds))
                for i, w in enumerate(workers)}
        self.jobs[job_id] = {"title": title, "description": description, "budget": D(budget), "bids": bids,
                             "state": "open", "worker": None, "revisions": 0, "held": Decimal(0), "amount": Decimal(0)}
        return job_id

    def list_bids(self, job_id):
        return [b for _, b in self._job(job_id)["bids"].values()]

    def counter(self, job_id, bid_id, amount, reasoning):
        job = self._job(job_id)
        w, bid = job["bids"][bid_id]
        self.counters.append({"job": job_id, "bid": bid_id, "amount": str(amount), "reasoning": reasoning})
        if D(amount) >= up(job["budget"] * w.floor_factor):
            revised = Bid(bid.bid_id, bid.bidder, D(amount), bid.eta_seconds)
            job["bids"][bid_id] = (w, revised)
            return revised
        return None

    def award(self, job_id, bid_id):
        job = self._job(job_id)
        w, bid = job["bids"][bid_id]
        hold = bid.amount + up(bid.amount * self.fee_rate)
        if self.balance < hold:
            raise MarketError("insufficient market wallet balance for escrow")
        self.balance -= hold
        job.update(state="awarded", worker=w, held=hold, amount=bid.amount)

    def get_submission(self, job_id) -> Optional[Submission]:
        job = self._job(job_id)
        if job["state"] not in ("awarded", "revising"):
            return None
        w = job["worker"]
        criteria = _criteria(job["description"])
        good = (w.quality == "good" or (w.quality == "revises" and job["revisions"] >= 1)
                or w.quality == "badhash")
        text = _good_text(job["title"], criteria) if good else f"{job['title']}: draft, see later."
        proof = sha256_text(text) if w.quality != "badhash" else sha256_text("something else")
        return Submission(text, proof)

    def request_revision(self, job_id, feedback):
        job = self._job(job_id)
        job["revisions"] += 1
        job["state"] = "revising"

    def accept(self, job_id):
        job = self._job(job_id)
        job["state"] = "closed"

    def dispute(self, job_id, reason):
        self._job(job_id)["state"] = "disputed"

    def _job(self, job_id):
        if job_id not in self.jobs:
            raise MarketError(f"unknown job {job_id}")
        return self.jobs[job_id]


class MockWallet:
    """Stands in for the Agent API wallet. Credits the mock market's wallet on withdraw."""

    def __init__(self, market: MockMarket, balance):
        self.market, self.balance, self.sent = market, D(balance), []

    def quote_withdraw(self, amount, destination):
        warnings = []
        if D(amount) > self.balance:
            warnings.append("insufficient_balance")
        if destination != self.market.deposit_address():
            warnings.append("policy_destination_denied")
        return WalletQuote(ok=True, warnings=warnings)

    def withdraw(self, amount, destination, idempotency_key):
        if any(s["key"] == idempotency_key for s in self.sent):
            return Execution(f"op_mock_{idempotency_key[:8]}", "SUCCESS", True)
        self.balance -= D(amount)
        self.market.credit(amount)
        self.sent.append({"key": idempotency_key, "amount": str(amount), "to": destination})
        return Execution(f"op_mock_{idempotency_key[:8]}", "SUCCESS", True)


def _criteria(description: str) -> dict:
    m = re.search(r"ACCEPTANCE_CRITERIA_JSON: (\{.*\})", description)
    return json.loads(m.group(1)) if m else {}


def _good_text(title: str, criteria: dict) -> str:
    terms = criteria.get("required_terms", [])
    sentence = f"This section on {title} covers " + ", ".join(terms) + "." if terms else f"Notes on {title}."
    need = int(criteria.get("min_words", 20))
    body = [sentence]
    while sum(len(s.split()) for s in body) < need + 5:
        body.append("Supporting detail follows the agreed scope and is written for a general reader.")
    return f"# {title}\n\n" + " ".join(body)
