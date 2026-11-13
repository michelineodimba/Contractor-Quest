"""The task loop: make or buy each part, negotiate, fund escrow, check, pay, and write a receipt."""
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from .checker import run_checks, sha256_text
from .ledger import BudgetLedger
from .models import Plan, Receipt, Task
from .money import D, fmt, max_bid_for_cap, up
from .negotiation import negotiate
from .policy import PolicyViolation
from .receipts import ReceiptStore


@dataclass
class RunConfig:
    fee_rate: Decimal = Decimal("0.02")   # ASSUMPTION: ~2% marketplace fee seen in public job records, paid on top
    bid_window_s: float = 0
    submit_window_s: float = 0
    funding_window_s: float = 0
    poll_s: float = 5
    max_bids: int = 5
    max_revisions: int = 1
    pace: float = 0                        # seconds to pause after each part (watchable demos)


@dataclass
class RunReport:
    run_id: str
    budget: Decimal
    currency: str
    spent: Decimal
    held: Decimal
    remaining: Decimal
    receipts: List[Receipt] = field(default_factory=list)
    result_path: Optional[Path] = None


class Contractor:
    def __init__(self, *, market, treasury, ledger: BudgetLedger, executor, store: ReceiptStore, run_id: str,
                 judge=None, cfg: Optional[RunConfig] = None, sleep=time.sleep, clock=time.monotonic):
        self.market, self.treasury, self.ledger = market, treasury, ledger
        self.executor, self.store, self.run_id, self.judge = executor, store, run_id, judge
        self.cfg, self.sleep, self.clock = cfg or RunConfig(), sleep, clock

    # ---- main loop -------------------------------------------------------------------------
    def run(self, task: Task, plan: Plan) -> RunReport:
        cur = plan.currency
        outputs = {}
        for part in plan.parts:
            if part.mode == "make":
                receipt, text = self._make(part, outputs, cur)
            else:
                receipt, text = self._buy(task, part, cur)
            self.store.add(receipt)
            if self.cfg.pace:
                self.sleep(self.cfg.pace)
            if text is not None and receipt.status in ("paid", "self_done"):
                outputs[part.id] = text

        result = self.store.dir / "result.md"
        body = [f"# {plan.task_title}\n"]
        for p in plan.parts:
            body.append(outputs.get(p.id, f"## {p.title}\n\n_Not delivered (see receipts)._\n"))
        result.write_text("\n\n".join(body))

        self.store.write_markdown(cur, str(plan.budget), str(self.ledger.total_spent), str(self.ledger.total_reserved))
        return RunReport(self.run_id, plan.budget, cur, self.ledger.total_spent, self.ledger.total_reserved,
                         self.ledger.available(), list(self.store.receipts), result)

    # ---- make ------------------------------------------------------------------------------
    def _make(self, part, outputs, cur) -> Tuple[Receipt, Optional[str]]:
        r = Receipt(self.run_id, part.id, part.title, "make", "error", "self", cur)
        try:
            inputs = {d: outputs[d] for d in part.depends_on if d in outputs}
            text = self.executor.run(part, inputs)
            chk = run_checks(part, text, None, self.judge)
            r.check = vars(chk).copy()
            r.deliverable_sha256 = sha256_text(text)
            r.status = "self_done" if chk.passed else "failed_check"
            return r, text
        except Exception as e:
            r.notes.append(f"{type(e).__name__}: {e}")
            return r, None

    # ---- buy -------------------------------------------------------------------------------
    def _buy(self, task, part, cur) -> Tuple[Receipt, Optional[str]]:
        r = Receipt(self.run_id, part.id, part.title, "buy", "error", "-", cur)
        try:
            return self._buy_inner(task, part, cur, r)
        except Exception as e:
            r.notes.append(f"{type(e).__name__}: {e}")
            if not r.payments:
                self.ledger.release(part.id)      # nothing left our wallet, so free the reservation
            else:
                r.status = "unresolved"            # money moved: keep it reserved until a human looks
            return r, None

    def _buy_inner(self, task, part, cur, r) -> Tuple[Receipt, Optional[str]]:
        cfg = self.cfg
        self.ledger.reserve(part.id, part.cap)
        ceiling = max_bid_for_cap(part.cap, cfg.fee_rate)
        if ceiling <= 0:
            raise PolicyViolation("cap too small to buy anything")

        job_id = self.market.post_job(part.title, self._job_description(task, part, ceiling, cur),
                                      ceiling, [f"part:{part.id}"])
        r.market_job_id = job_id
        bids = self._poll(lambda: self.market.list_bids(job_id), cfg.bid_window_s,
                          lambda b: len(b) >= cfg.max_bids)
        if bids:
            r.quoted = str(min(b.amount for b in bids))

        outcome = negotiate(self.market, job_id, bids, ceiling, part.title, cur)
        r.negotiation = outcome.steps
        if outcome.accepted is None:
            r.status = "no_deal"
            self.ledger.release(part.id)
            return r, None

        bid = outcome.accepted
        if bid.amount > ceiling:                       # defence in depth; negotiate() already guarantees this
            raise PolicyViolation("accepted bid above ceiling")
        fee = up(bid.amount * cfg.fee_rate)
        total = bid.amount + fee
        if total > part.cap:
            raise PolicyViolation("bid plus fee exceeds the part cap")
        r.seller, r.bid_id, r.final_price, r.fee = bid.bidder, bid.bid_id, str(bid.amount), str(fee)

        need = total - self.market.wallet_balance()
        if need > 0:
            ex = self.treasury.pay(part_id=part.id, recipient=self.market.deposit_address(), amount=need,
                                   currency=cur, purpose=f"escrow for {part.id}")
            r.payments.append({"op_id": ex.op_id, "status": ex.status, "amount": str(up(need)),
                               "to": self.market.deposit_address()})
            funded = self._poll(self.market.wallet_balance, cfg.funding_window_s, lambda b: b >= total)
            if funded < total:
                raise RuntimeError("market wallet was not credited in time")

        self.market.award(job_id, bid.bid_id)
        sub = self._poll(lambda: self.market.get_submission(job_id), cfg.submit_window_s, lambda s: s is not None)
        if sub is None:
            r.status = "unresolved"
            r.notes.append("no submission before the deadline; funds stay in escrow")
            return r, None

        chk = run_checks(part, sub.content, sub.proof_sha256, self.judge)
        revisions = 0
        while not chk.passed and revisions < cfg.max_revisions:
            self.market.request_revision(job_id, "; ".join(chk.reasons))
            revisions += 1
            before = sha256_text(sub.content)
            sub = self._poll(lambda: self.market.get_submission(job_id), cfg.submit_window_s,
                             lambda s: s is not None and sha256_text(s.content) != before) or sub
            chk = run_checks(part, sub.content, sub.proof_sha256, self.judge)
        r.check = vars(chk).copy()
        r.deliverable_sha256 = sha256_text(sub.content)
        if revisions:
            r.notes.append(f"{revisions} revision request(s) sent")

        if chk.passed:
            self.market.accept(job_id)
            self.ledger.commit(part.id, total)
            r.total_paid, r.status = str(total), "paid"
            return r, sub.content

        self.market.dispute(job_id, "; ".join(chk.reasons))
        r.status = "withheld"
        r.notes.append("check failed after revision; payment withheld and dispute opened; funds stay reserved")
        return r, None

    # ---- helpers ---------------------------------------------------------------------------
    def _job_description(self, task, part, ceiling, cur) -> str:
        return (f"{part.spec}\n\nContext: part of '{task.title}'.\n"
                f"Maximum budget: {fmt(ceiling, cur)}. Escrow is funded on award and released on acceptance.\n"
                f"ACCEPTANCE_CRITERIA_JSON: {json.dumps(part.checks, sort_keys=True)}")

    def _poll(self, fn, window, done):
        deadline = self.clock() + window
        while True:
            val = fn()
            if done(val) or self.clock() >= deadline:
                return val
            self.sleep(self.cfg.poll_s)
