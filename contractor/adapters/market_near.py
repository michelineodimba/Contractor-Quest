"""Adapter for the NEAR AI Agent Market REST API (https://market.near.ai, /v1/*, Bearer API key).

Confirmed from the public skill doc: register (no auth), GET /v1/jobs, POST /v1/jobs/{id}/bids,
POST /v1/jobs/{id}/award, POST /v1/jobs/{id}/accept, GET /v1/wallet/balance.
Everything marked VERIFY is an assumption about request or response shape that I could not read.
Run `python -m contractor verify` and adjust the one place marked, rather than the orchestrator.
"""
from decimal import Decimal
from typing import List, Optional

from ..http import Http
from ..models import Bid, Submission
from ..money import D
from .base import MarketError


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


class NearMarket:
    name = "market.near.ai"

    def __init__(self, base_url: str, api_key: str, deposit_address: str, token: str = "USDC",
                 http: Optional[Http] = None, counter_wait_s: float = 120, poll_s: float = 5,
                 sleep=None, clock=None):
        import time
        self.http = http or Http(base_url, {"Authorization": f"Bearer {api_key}"})
        self._deposit, self.token = deposit_address, token
        self.counter_wait_s, self.poll_s = counter_wait_s, poll_s
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic

    def deposit_address(self) -> str:
        # The address the Agent API wallet must be allowed to withdraw to. Set from a verified source.
        return self._deposit

    def wallet_balance(self) -> Decimal:
        r = self.http.request("GET", "/v1/wallet/balance")                      # confirmed endpoint
        return D(_first(r, "balance", "available", "available_balance", default=0))   # VERIFY field name

    def post_job(self, title, description, budget, tags) -> str:
        r = self.http.request("POST", "/v1/jobs", json_body={                   # VERIFY body fields
            "title": title, "description": description,
            "budget_amount": str(budget), "budget_token": self.token, "tags": tags})
        return str(_first(r, "job_id", "id"))

    def list_bids(self, job_id) -> List[Bid]:
        r = self.http.request("GET", f"/v1/jobs/{job_id}/bids")                # VERIFY path and shape
        items = r if isinstance(r, list) else _first(r, "bids", "items", default=[])
        return [Bid(str(_first(b, "bid_id", "id")), str(_first(b, "bidder", "agent_name", "agent_id", default="?")),
                    D(_first(b, "amount", "price")), int(_first(b, "eta_seconds", default=0) or 0),
                    str(_first(b, "proposal", default="")))
                for b in items]

    def counter(self, job_id, bid_id, amount, reasoning) -> Optional[Bid]:
        # VERIFY: no native counter endpoint found. Send the offer as a job message, then watch for a revised bid.
        self.http.request("POST", f"/v1/jobs/{job_id}/messages", json_body={"body": reasoning})
        deadline = self._clock() + self.counter_wait_s
        while True:
            for b in self.list_bids(job_id):
                if b.bid_id == bid_id and b.amount <= D(amount):
                    return b
            if self._clock() >= deadline:
                return None
            self._sleep(self.poll_s)

    def award(self, job_id, bid_id) -> None:
        self.http.request("POST", f"/v1/jobs/{job_id}/award", json_body={"bid_id": bid_id})   # body VERIFY

    def get_submission(self, job_id) -> Optional[Submission]:
        job = self.http.request("GET", f"/v1/jobs/{job_id}")                   # VERIFY: where the deliverable lives
        sub = _first(job, "submission", "deliverable", "result")
        if not sub:
            return None
        if isinstance(sub, str):
            return Submission(sub)
        return Submission(str(_first(sub, "content", "text", "body", default="")),
                          _first(sub, "hash", "sha256", "proof"))

    def request_revision(self, job_id, feedback) -> None:
        self.http.request("POST", f"/v1/jobs/{job_id}/messages", json_body={"body": f"Revision requested: {feedback}"})

    def accept(self, job_id) -> None:
        self.http.request("POST", f"/v1/jobs/{job_id}/accept")                  # confirmed endpoint

    def dispute(self, job_id, reason) -> None:
        self.http.request("POST", f"/v1/jobs/{job_id}/dispute", json_body={"reason": reason})  # VERIFY
