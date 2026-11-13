"""Builds a ready-to-run Contractor for demo (simulated) or live mode. Shared by the CLI and the web UI."""
import json
import os
import time
from pathlib import Path

from .adapters.mock import MockMarket, MockWallet, MockWorker
from .config import LiveSettings
from .contractor import Contractor, RunConfig
from .ledger import BudgetLedger
from .llm import LLMExecutor, LLMJudge, OpenAICompatLLM, TemplateExecutor
from .money import D
from .planner import build_plan, plan_hash, task_from_json
from .policy import SpendPolicy, Treasury
from .receipts import ReceiptStore

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "launch_brief.json"


class ApprovalError(RuntimeError):
    def __init__(self, plan_hash_value):
        self.plan_hash = plan_hash_value
        super().__init__(f"Not approved. Re-run with approval of plan hash {plan_hash_value} to authorise exactly this plan and budget.")


def parse_task(text: str, budget=None):
    return task_from_json(json.loads(text), budget=budget or None)


def llm_from_env():
    if all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")):
        return OpenAICompatLLM(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"], os.environ["LLM_MODEL"])
    return None


def make_demo(task, out_root, pace=0):
    plan = build_plan(task)
    scenarios = {
        "market_data": [MockWorker("data-agent-a", D("1.40"), D("0.95")),
                        MockWorker("data-agent-b", D("1.60"), D("1.30"))],
        "competitor_scan": [MockWorker("scan-agent", D("0.90"), D("0.90"), quality="revises")],
        "risk_review": [MockWorker("risk-agent", D("0.80"), D("0.80"), quality="bad")],
    }
    market = MockMarket(scenarios)
    run_id = time.strftime("demo-%Y%m%d-%H%M%S")
    out = Path(out_root) / run_id
    ledger = BudgetLedger(plan.budget, plan.currency, out / "ledger.jsonl")
    wallet = MockWallet(market, plan.budget)
    policy = SpendPolicy(frozenset([market.deposit_address()]), plan.currency, max(p.cap for p in plan.parts))
    c = Contractor(market=market, treasury=Treasury(wallet, ledger, policy, run_id), ledger=ledger,
                   executor=TemplateExecutor(), store=ReceiptStore(out), run_id=run_id,
                   cfg=RunConfig(pace=pace))
    return plan, c


def make_live(task, out_root, approve, judge=False, bid_window=300, submit_window=3600):
    from .adapters.agent_wallet import AgentApiWallet
    from .adapters.market_near import NearMarket
    s = LiveSettings.from_env()
    task.currency = s.currency
    plan = build_plan(task)
    h = plan_hash(plan)
    if approve != h:
        raise ApprovalError(h)
    run_id = time.strftime("run-%Y%m%d-%H%M%S")
    out = Path(out_root) / run_id
    ledger = BudgetLedger(plan.budget, plan.currency, out / "ledger.jsonl")
    market = NearMarket(s.market_url, s.market_key, s.deposit_address, token=s.currency)
    wallet = AgentApiWallet(s.agent_api_url, s.agent_api_key, s.grant_token, s.agent_id, s.asset_id, s.decimals)
    policy = SpendPolicy(frozenset([s.deposit_address]), plan.currency, max(p.cap for p in plan.parts))
    llm = llm_from_env()
    c = Contractor(market=market, treasury=Treasury(wallet, ledger, policy, run_id), ledger=ledger,
                   executor=LLMExecutor(llm) if llm else TemplateExecutor(), store=ReceiptStore(out),
                   run_id=run_id, judge=LLMJudge(llm) if (llm and judge) else None,
                   cfg=RunConfig(fee_rate=s.fee_rate, bid_window_s=bid_window, submit_window_s=submit_window,
                                 funding_window_s=120))
    return plan, c


def live_env_missing():
    names = ["NEAR_MARKET_API_KEY", "MARKET_DEPOSIT_ADDRESS", "NEAR_INTENTS_AGENT_API_KEY",
             "AGENT_ID", "AGENT_GRANT_TOKEN"]
    return [n for n in names if not os.environ.get(n)]


def run_probes(settings=None):
    """Read-only calls plus one DRY withdraw quote. Moves no funds."""
    from .http import Http
    s = settings or LiveSettings.from_env()
    market = Http(s.market_url, {"Authorization": f"Bearer {s.market_key}"})
    agent = Http(s.agent_api_url, {"X-API-Key": s.agent_api_key, "X-Grant-Token": s.grant_token})
    probes = [
        ("Market: list open jobs", lambda: market.request("GET", "/v1/jobs", params={"status": "open", "limit": 1})),
        ("Market: wallet balance", lambda: market.request("GET", "/v1/wallet/balance")),
        ("Agent API: network", lambda: agent.request("GET", "/v1/network")),
        ("Agent API: supported tokens", lambda: agent.request("GET", "/v1/tokens")),
        ("Agent API: dry withdraw quote", lambda: agent.request(
            "POST", f"/v1/agents/{s.agent_id}/withdraw",
            json_body={"asset_id": s.asset_id, "amount": "10000", "destination": s.deposit_address, "dry": True})),
    ]
    out = []
    for name, fn in probes:
        try:
            out.append({"name": name, "ok": True, "detail": json.dumps(fn())[:400]})
        except Exception as e:
            out.append({"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}"[:400]})
    return out
