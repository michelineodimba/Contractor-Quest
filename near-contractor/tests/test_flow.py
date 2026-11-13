import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec
from pathlib import Path
from unittest import mock

from contractor import cli, runner
from contractor.adapters.agent_wallet import AgentApiWallet, to_atomic
from contractor.adapters.market_near import NearMarket
from contractor.adapters.mock import MockMarket, MockWallet, MockWorker
from contractor.contractor import Contractor
from contractor.http import Http
from contractor.ledger import BudgetLedger
from contractor.llm import TemplateExecutor
from contractor.money import D
from contractor.planner import build_plan, task_from_json
from contractor.policy import SpendPolicy, Treasury
from contractor.receipts import ReceiptStore


def build(budget="20", recipients=None):
    task = task_from_json(json.loads(Path(runner.EXAMPLE).read_text()), budget=budget)
    plan = build_plan(task)
    market = MockMarket({
        "market_data": [MockWorker("a", D("1.4"), D("0.95")), MockWorker("b", D("1.6"), D("1.3"))],
        "competitor_scan": [MockWorker("c", D("0.9"), D("0.9"), quality="revises")],
        "risk_review": [MockWorker("d", D("0.8"), D("0.8"), quality="bad")],
    })
    out = Path(tempfile.mkdtemp())
    ledger = BudgetLedger(plan.budget, plan.currency)
    wallet = MockWallet(market, plan.budget)
    allowed = recipients if recipients is not None else [market.deposit_address()]
    policy = SpendPolicy(frozenset(allowed), plan.currency, max(p.cap for p in plan.parts))
    c = Contractor(market=market, treasury=Treasury(wallet, ledger, policy, "t"), ledger=ledger,
                   executor=TemplateExecutor(), store=ReceiptStore(out), run_id="t")
    return task, plan, market, wallet, ledger, c


class TestEndToEnd(unittest.TestCase):
    def test_every_part_gets_a_receipt_and_spend_stays_inside_budget(self):
        task, plan, market, wallet, ledger, c = build()
        rep = c.run(task, plan)
        self.assertEqual([r.part_id for r in rep.receipts], [p.id for p in plan.parts])
        self.assertLessEqual(rep.spent + rep.held, plan.budget)
        self.assertLessEqual(sum(D(s["amount"]) for s in wallet.sent), plan.budget)
        by = {r.part_id: r for r in rep.receipts}
        self.assertEqual(by["market_data"].status, "paid")
        self.assertEqual(by["competitor_scan"].status, "paid")
        self.assertEqual(by["risk_review"].status, "withheld")
        self.assertEqual(by["risk_review"].total_paid, "0")
        for r in rep.receipts:
            self.assertIn("passed", r.check)

    def test_one_counter_only_per_part(self):
        task, plan, market, wallet, ledger, c = build()
        c.run(task, plan)
        per_job = [x["job"] for x in market.counters]
        self.assertEqual(len(per_job), len(set(per_job)))
        self.assertEqual(len(market.counters), 1)

    def test_accepted_price_never_above_part_cap(self):
        task, plan, market, wallet, ledger, c = build()
        rep = c.run(task, plan)
        caps = {p.id: p.cap for p in plan.parts}
        for r in rep.receipts:
            self.assertLessEqual(D(r.final_price) + D(r.fee), caps[r.part_id])

    def test_unlisted_recipient_moves_no_money_and_frees_reservation(self):
        task, plan, market, wallet, ledger, c = build(recipients=["someone-else.near"])
        rep = c.run(task, plan)
        self.assertEqual(wallet.sent, [])
        self.assertEqual(rep.spent, 0)
        self.assertEqual(rep.held, 0)
        self.assertTrue(all(r.status in ("error", "self_done") for r in rep.receipts))

    def test_withheld_part_does_not_commit_spend(self):
        task, plan, market, wallet, ledger, c = build()
        c.run(task, plan)
        self.assertNotIn("risk_review", ledger.spent)
        self.assertIn("risk_review", ledger.reserved)


def fake_send(log, responses):
    def send(method, url, headers, data, timeout):
        log.append((method, url, dict(headers), json.loads(data) if data else None))
        for (m, frag), (status, body) in responses.items():
            if m == method and frag in url:
                return status, json.dumps(body).encode()
        return 404, b"{}"
    return send


class TestAdapters(unittest.TestCase):
    def wallet(self, responses):
        log = []
        h = Http("https://x", {"X-API-Key": "k", "X-Grant-Token": "g"}, send=fake_send(log, responses))
        return AgentApiWallet("https://x", "k", "g", "agent1", "nep141:usdc", 6, http=h), log

    def test_to_atomic(self):
        self.assertEqual(to_atomic(D("1.5"), 6), "1500000")

    def test_dry_has_flag_and_no_idempotency_key_and_live_has_key(self):
        w, log = self.wallet({("POST", "/withdraw"): (200, {"correlation_id": "op_1", "warnings": []}),
                              ("GET", "/v1/status"): (200, {"status": "SUCCESS"})})
        self.assertTrue(w.quote_withdraw(D(1), "dest").ok)
        ex = w.withdraw(D(1), "dest", "idem-1")
        self.assertTrue(ex.ok)
        dry, live = log[0], log[1]
        self.assertTrue(dry[3]["dry"])
        self.assertNotIn("Idempotency-Key", dry[2])
        self.assertEqual(live[2]["Idempotency-Key"], "idem-1")
        self.assertNotIn("dry", live[3])
        self.assertEqual(live[2]["X-Grant-Token"], "g")

    def test_unknown_status_fails_closed(self):
        w, _ = self.wallet({("POST", "/withdraw"): (200, {"correlation_id": "op_1"}),
                            ("GET", "/v1/status"): (200, {"status": "WHATEVER"})})
        self.assertFalse(w.withdraw(D(1), "dest", "k").ok)

    def test_warnings_are_surfaced(self):
        w, _ = self.wallet({("POST", "/withdraw"): (200, {"warnings": [{"code": "spend_budget_exceeded"}]})})
        q = w.quote_withdraw(D(1), "dest")
        self.assertEqual(q.warnings, ["spend_budget_exceeded"])

    def test_market_parses_bids_and_accepts(self):
        log = []
        h = Http("https://m", {"Authorization": "Bearer k"}, send=fake_send(log, {
            ("GET", "/bids"): (200, [{"id": "b1", "agent_name": "w", "amount": "3.5", "eta_seconds": 60}]),
            ("POST", "/accept"): (200, {})}))
        m = NearMarket("https://m", "k", "dep", http=h)
        bids = m.list_bids("j1")
        self.assertEqual((bids[0].bid_id, bids[0].amount), ("b1", D("3.5")))
        m.accept("j1")
        self.assertTrue(log[-1][1].endswith("/v1/jobs/j1/accept"))


class TestCliGate(unittest.TestCase):
    def test_live_run_refuses_without_matching_approval(self):
        env = {"NEAR_MARKET_API_KEY": "k", "MARKET_DEPOSIT_ADDRESS": "d", "NEAR_INTENTS_AGENT_API_KEY": "k",
               "AGENT_ID": "a", "AGENT_GRANT_TOKEN": "g"}
        with mock.patch.dict(os.environ, env), mock.patch("contractor.http.Http._urllib_send",
                                                          side_effect=AssertionError("network touched")):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["run", "--approve", "wrong"])
            self.assertIn("Not approved", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class TestUI(unittest.TestCase):
    def setUp(self):
        import threading, urllib.request
        from contractor import ui
        self.srv, self.st, self.token = ui.make_server(0, tempfile.mkdtemp())
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.urllib = urllib.request

    def tearDown(self):
        self.srv.shutdown()

    def call(self, path, body=None, token=True, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-UI-Token"] = self.token
        data = json.dumps(body).encode() if body is not None else None
        req = self.urllib.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with self.urllib.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except Exception as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_requires_token_and_good_host(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.com")[0], 403)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_plan_and_demo_run(self):
        import time
        task = Path(runner.EXAMPLE).read_text()
        code, plan = self.call("/api/plan", {"task": task, "budget": "20"})
        self.assertEqual(code, 200)
        self.assertEqual(len(plan["hash"]), 16)
        self.assertEqual(self.call("/api/run", {"mode": "demo", "task": task, "budget": "20"})[0], 202)
        for _ in range(100):
            code, s = self.call("/api/state")
            if s["done"]:
                break
            time.sleep(0.05)
        self.assertIsNone(s["error"])
        self.assertEqual(len(s["receipts"]), 5)
        self.assertLessEqual(float(s["totals"]["spent"]) + float(s["totals"]["held"]), 20)
        self.assertIn("Launch brief", s["result"])

    def test_live_refused_without_config_or_approval(self):
        import time
        task = Path(runner.EXAMPLE).read_text()
        with mock.patch.dict(os.environ, {"NEAR_MARKET_API_KEY": "k", "MARKET_DEPOSIT_ADDRESS": "d",
                                          "NEAR_INTENTS_AGENT_API_KEY": "k", "AGENT_ID": "a", "AGENT_GRANT_TOKEN": "g"}):
            self.call("/api/run", {"mode": "live", "task": task, "budget": "5", "approve": "wrong"})
            for _ in range(100):
                _, s = self.call("/api/state")
                if s["done"]:
                    break
                time.sleep(0.05)
        self.assertIn("Not approved", s["error"])
        self.assertEqual(s["receipts"], [])


class TestUIWalletAndConnect(TestUI):
    def test_snapshot_has_wallet_with_payments_after_demo(self):
        import time
        task = Path(runner.EXAMPLE).read_text()
        self.call("/api/run", {"mode": "demo", "task": task, "budget": "20"})
        for _ in range(100):
            _, s = self.call("/api/state")
            if s["done"]:
                break
            time.sleep(0.05)
        w = s["wallet"]
        self.assertEqual(w["kind"], "simulated")
        self.assertEqual(len(w["payments"]), 3)
        self.assertEqual(w["allowed_recipients"], ["mock-market.near"])
        self.assertLessEqual(D(w["budget"]) - D(w["balance"]), D(w["budget"]))

    def test_connect_stores_only_known_keys_and_never_echoes_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            code, body = self.call("/api/connect", {"NEAR_MARKET_API_KEY": "secret-123", "AGENT_ID": "a1",
                                                    "EVIL_VAR": "x", "AGENT_GRANT_TOKEN": "bad\nvalue"})
            self.assertEqual(code, 200)
            self.assertNotIn("secret-123", json.dumps(body))
            self.assertTrue(body["connected"]["NEAR_MARKET_API_KEY"])
            self.assertNotIn("EVIL_VAR", os.environ)
            self.assertFalse(body["connected"]["AGENT_GRANT_TOKEN"])
            _, s = self.call("/api/state")
            self.assertNotIn("secret-123", json.dumps(s))
            for k in ("NEAR_MARKET_API_KEY", "AGENT_ID"):
                os.environ.pop(k, None)

    def test_verify_endpoint_reports_missing_config(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, body = self.call("/api/verify", {})
        self.assertEqual(code, 200)
        self.assertIn("missing environment variables", body["error"])
