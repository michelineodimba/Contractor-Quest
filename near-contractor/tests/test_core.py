import random
import unittest
from decimal import Decimal as Dec

from contractor.adapters.base import Execution, WalletQuote
from contractor.checker import run_checks, sha256_text
from contractor.ledger import BudgetError, BudgetLedger
from contractor.models import Bid, CheckResult, Part, Task
from contractor.money import D, Q, max_bid_for_cap, up
from contractor.negotiation import negotiate
from contractor.planner import LLMDecomposer, PlanError, build_plan, plan_hash, task_from_json
from contractor.policy import PolicyViolation, SpendPolicy, Treasury


class TestMoney(unittest.TestCase):
    def test_max_bid_is_tight_and_never_exceeds_cap(self):
        for cap in ["0.5", "3", "9", "17.123457", "100"]:
            for fee in ["0", "0.02", "0.025", "0.1"]:
                cap_d, fee_d = D(cap), D(fee)
                bid = max_bid_for_cap(cap_d, fee_d)
                self.assertLessEqual(bid + up(bid * fee_d), cap_d)
                nxt = bid + Q
                self.assertGreater(nxt + up(nxt * fee_d), cap_d)


class TestLedger(unittest.TestCase):
    def test_basic_flow(self):
        l = BudgetLedger(D(10), "USDC")
        l.reserve("a", 6)
        l.commit("a", 5)
        self.assertEqual(l.available(), D(5))
        l.reserve("b", 5)
        with self.assertRaises(BudgetError):
            l.reserve("c", "0.000001")
        l.release("b")
        self.assertEqual(l.available(), D(5))

    def test_cannot_commit_more_than_reserved_or_without_reservation(self):
        l = BudgetLedger(D(10), "USDC")
        l.reserve("a", 2)
        with self.assertRaises(BudgetError):
            l.commit("a", 3)
        with self.assertRaises(BudgetError):
            l.commit("zzz", 1)

    def test_random_operations_never_exceed_total(self):
        rnd = random.Random(7)
        l = BudgetLedger(D(50), "USDC")
        for i in range(400):
            k = f"p{rnd.randint(0, 25)}"
            try:
                op = rnd.choice(["reserve", "commit", "release"])
                if op == "reserve":
                    l.reserve(k, D(rnd.randint(1, 900)) / 100)
                elif op == "commit":
                    l.commit(k, D(rnd.randint(1, 900)) / 100)
                else:
                    l.release(k)
            except BudgetError:
                pass
            self.assertLessEqual(l.total_reserved + l.total_spent, l.total)


class FakeWallet:
    def __init__(self, warnings=None, ok=True):
        self.warnings, self.ok, self.calls = warnings or [], ok, []

    def quote_withdraw(self, amount, destination):
        return WalletQuote(True, list(self.warnings))

    def withdraw(self, amount, destination, key):
        self.calls.append((amount, destination, key))
        return Execution("op_1", "SUCCESS" if self.ok else "FAILED", self.ok)


class TestTreasury(unittest.TestCase):
    def setUp(self):
        self.ledger = BudgetLedger(D(20), "USDC")
        self.ledger.reserve("p1", 5)
        self.wallet = FakeWallet()
        self.t = Treasury(self.wallet, self.ledger, SpendPolicy(frozenset(["market.near"]), "USDC", D(5)), "run1")

    def pay(self, **kw):
        a = dict(part_id="p1", recipient="market.near", amount=3, currency="USDC", purpose="x")
        a.update(kw)
        return self.t.pay(**a)

    def test_happy_path(self):
        self.assertTrue(self.pay().ok)
        self.assertEqual(len(self.wallet.calls), 1)

    def test_refusals_never_reach_wallet(self):
        for kw in [dict(recipient="evil.near"), dict(currency="NEAR"), dict(amount=6),
                   dict(part_id="nope"), dict(amount=0)]:
            with self.assertRaises(PolicyViolation):
                self.pay(**kw)
        self.assertEqual(self.wallet.calls, [])

    def test_cannot_exceed_reservation_across_payments(self):
        self.pay(amount=3)
        with self.assertRaises(PolicyViolation):
            self.pay(amount=3)

    def test_preflight_warning_blocks(self):
        self.wallet.warnings = ["spend_budget_exceeded"]
        with self.assertRaises(PolicyViolation):
            self.pay()
        self.assertEqual(self.wallet.calls, [])

    def test_failed_execution_raises(self):
        self.wallet.ok = False
        with self.assertRaises(PolicyViolation):
            self.pay()


class FakeMarket:
    def __init__(self, revised=None):
        self.revised, self.counters = revised, []

    def counter(self, job_id, bid_id, amount, reasoning):
        self.counters.append((bid_id, amount, reasoning))
        return self.revised


class TestNegotiation(unittest.TestCase):
    def bid(self, i, amt):
        return Bid(f"b{i}", f"w{i}", D(amt), 100)

    def test_within_ceiling_accepts_cheapest_without_countering(self):
        m = FakeMarket()
        r = negotiate(m, "j", [self.bid(1, 9), self.bid(2, 7), self.bid(3, 12)], D(10), "t", "USDC")
        self.assertEqual(r.accepted.bidder, "w2")
        self.assertEqual(m.counters, [])

    def test_counters_exactly_once_then_accepts_if_met(self):
        m = FakeMarket(revised=self.bid(1, 10))
        r = negotiate(m, "j", [self.bid(1, 14), self.bid(2, 15)], D(10), "t", "USDC")
        self.assertEqual(len(m.counters), 1)
        self.assertEqual(r.accepted.amount, D(10))
        self.assertIn("single counter", m.counters[0][2])

    def test_walks_away_if_no_reply_or_still_too_high(self):
        for revised in (None, self.bid(1, 12)):
            m = FakeMarket(revised=revised)
            r = negotiate(m, "j", [self.bid(1, 14)], D(10), "t", "USDC")
            self.assertIsNone(r.accepted)
            self.assertEqual(len(m.counters), 1)

    def test_no_bids(self):
        self.assertIsNone(negotiate(FakeMarket(), "j", [], D(10), "t", "USDC").accepted)

    def test_reasoning_does_not_leak_total_budget(self):
        m = FakeMarket()
        negotiate(m, "j", [self.bid(1, 14)], D(10), "t", "USDC")
        self.assertNotIn("total budget", m.counters[0][2].lower())


class TestChecker(unittest.TestCase):
    def part(self, **checks):
        return Part("p", "P", "spec", checks=checks)

    def test_required_terms_and_length(self):
        r = run_checks(self.part(min_words=3, required_terms=["alpha"]), "alpha beta gamma")
        self.assertTrue(r.passed)
        r = run_checks(self.part(min_words=10, required_terms=["alpha"]), "beta")
        self.assertFalse(r.passed)
        self.assertEqual(len(r.reasons), 2)

    def test_hash_proof_mismatch_fails(self):
        self.assertFalse(run_checks(self.part(), "text", sha256_text("other")).passed)
        self.assertTrue(run_checks(self.part(), "text", sha256_text("text")).passed)

    def test_json_keys(self):
        self.assertTrue(run_checks(self.part(json_keys=["a"]), '{"a": 1}').passed)
        self.assertFalse(run_checks(self.part(json_keys=["a"]), "nope").passed)

    def test_judge_can_veto_but_never_rescue(self):
        class Judge:
            def __init__(self, ok): self.ok, self.called = ok, 0
            def review(self, part, text):
                self.called += 1
                return CheckResult(self.ok, ["j"], "rubric")
        j = Judge(True)
        self.assertFalse(run_checks(self.part(required_terms=["x"]), "y", None, j).passed)
        self.assertEqual(j.called, 0)
        self.assertFalse(run_checks(self.part(), "fine", None, Judge(False)).passed)
        self.assertTrue(run_checks(self.part(), "fine", None, Judge(True)).passed)


class TestPlanner(unittest.TestCase):
    def task(self, budget="20"):
        return task_from_json({"title": "T", "budget": budget, "parts": [
            {"id": "a", "mode": "make"}, {"id": "b", "weight": 3}, {"id": "c", "weight": 1}]})

    def test_caps_plus_reserve_within_budget(self):
        plan = build_plan(self.task())
        self.assertLessEqual(plan.total_caps() + plan.reserve, plan.budget)
        self.assertEqual(plan.parts[0].cap, 0)
        self.assertEqual(plan.parts[1].cap, 3 * plan.parts[2].cap)

    def test_llm_cannot_set_money(self):
        class LLM:
            def complete(self, s, u):
                return ('{"parts":[{"id":"a","mode":"buy","cap":1000,"price":999,"weight":1},'
                        '{"id":"b","mode":"buy","weight":1,"budget":5000}]}')
        task = LLMDecomposer(LLM()).decompose("T", "d", "10", "USDC")
        plan = build_plan(task)
        self.assertLessEqual(plan.total_caps(), D(10))

    def test_bad_llm_output_is_rejected(self):
        class LLM:
            def complete(self, s, u): return "no json here"
        with self.assertRaises(PlanError):
            LLMDecomposer(LLM()).decompose("T", "d", "10", "USDC")

    def test_validation(self):
        with self.assertRaises(PlanError):
            build_plan(task_from_json({"title": "T", "budget": "1", "parts": [{"id": "a"}, {"id": "a"}]}))
        with self.assertRaises(PlanError):
            build_plan(task_from_json({"title": "T", "budget": "1", "parts": [
                {"id": "a", "depends_on": ["b"]}, {"id": "b"}]}))

    def test_plan_hash_binds_budget(self):
        self.assertNotEqual(plan_hash(build_plan(self.task("20"))), plan_hash(build_plan(self.task("21"))))


if __name__ == "__main__":
    unittest.main()
