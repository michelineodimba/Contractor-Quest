import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from .config import ConfigError, LiveSettings
from . import runner
from .http import Http, HttpError
from .ledger import BudgetLedger
from .llm import LLMExecutor, LLMJudge, OpenAICompatLLM, TemplateExecutor
from .money import D, fmt
from .planner import build_plan, describe_plan, plan_hash, task_from_json
from .policy import SpendPolicy, Treasury
from .receipts import ReceiptStore



def _load(path, budget):
    return runner.parse_task(Path(path).read_text(), budget)


def _print_report(rep):
    cur = rep.currency
    print(f"\nRun {rep.run_id}: budget {fmt(rep.budget, cur)} | spent {fmt(rep.spent, cur)} | "
          f"held (disputes/escrow) {fmt(rep.held, cur)} | unspent {fmt(rep.remaining, cur)}")
    for r in rep.receipts:
        chk = "passed" if r.check.get("passed") else "FAILED"
        print(f"  {r.part_id:<16} {r.status:<12} seller={r.seller:<12} quoted={r.quoted or '-':<10} "
              f"paid={r.total_paid:<10} check={chk}")
    print(f"Receipts: {rep.result_path.parent / 'receipts.md'}\nResult:   {rep.result_path}")


def cmd_demo(a):
    task = _load(a.task, a.budget)
    plan, c = runner.make_demo(task, a.out)
    print(describe_plan(plan))
    print("\n[SIMULATION: mock market and mock wallet. No network, no funds.]")
    _print_report(c.run(task, plan))


def cmd_plan(a):
    print(describe_plan(build_plan(_load(a.task, a.budget))))


def cmd_run(a):
    task = _load(a.task, a.budget)
    approve = a.approve
    while True:
        try:
            plan, c = runner.make_live(task, a.out, approve, a.judge, a.bid_window, a.submit_window)
            break
        except runner.ApprovalError as e:
            print(describe_plan(build_plan(task)))
            if approve or not sys.stdin.isatty():
                sys.exit(f"Not approved. Re-run with --approve {e.plan_hash} to authorise exactly this plan and budget.")
            approve = input("Spend real funds on exactly this plan? Type the plan hash to confirm: ").strip()
            if approve != e.plan_hash:
                sys.exit("Not confirmed. Nothing was spent.")
    print(describe_plan(plan))
    _print_report(c.run(task, plan))


def cmd_missions(a):
    from . import missions
    for i, m in enumerate(missions.MISSIONS, 1):
        print(f"{i}. {m['id']:<14} {'*' * m['difficulty']:<5} budget {m['budget']} {m['currency']}  {m['tagline']}")
        for p in m["parts"]:
            tag = "make/buy" if p["flex"] else p["mode"]
            print(f"     - {p['id']:<16} [{tag}] {p['intel'] or '(you write this)'}")


def cmd_play(a):
    """Play one game mission from the command line (simulated market) and print the score."""
    from . import game, missions
    draft = {}
    for pid in (a.make or "").split(","):
        if pid:
            draft.setdefault(pid, {})["mode"] = "make"
    for pid in (a.buy or "").split(","):
        if pid:
            draft.setdefault(pid, {})["mode"] = "buy"
    for item in a.stars or []:
        pid, _, n = item.partition("=")
        draft.setdefault(pid, {})["stars"] = n
    try:
        m, d, task, plan, c = runner.make_mission(a.mission, draft, a.out)
    except KeyError as e:
        sys.exit(str(e))
    unknown = set(draft) - {p["id"] for p in m["parts"]}
    if unknown:
        sys.exit(f"unknown part id(s): {', '.join(sorted(unknown))}")
    print(f"Mission: {m['name']}  (budget {m['budget']} {m['currency']})")
    print(describe_plan(plan))
    print("\n[SIMULATION: mock sellers and mock wallet. No network, no funds.]")
    rep = c.run(task, plan)
    _print_report(rep)
    result, receipts, flex_made = runner.score_mission(m, d, plan, c, rep)
    print("\nSCORE")
    for p in result["parts"]:
        for l in p["lines"]:
            print(f"  {l['points']:>+5}  {p['title']}: {l['label']}")
    for b in result["bonus"]:
        print(f"  {b['points']:>+5}  {b['label']}")
    print(f"  {'-' * 5}\n  {result['total']:>5}  TOTAL   grade {result['grade']}")
    if not a.no_save:
        prof = game.Profile(Path(a.out) / "profile.json")
        ap = prof.apply(m, result, receipts, a.name, flex_made)
        print(f"\n+{ap['xp_gain']} XP  ({ap['xp_total']} total)  rank: {ap['rank_after']}"
              + ("  RANK UP!" if ap["ranked_up"] else ""))
        for b in ap["new_badges"]:
            print(f"  badge unlocked: {b['name']}: {b['desc']}")
        print("Leaderboard:")
        for i, r in enumerate(ap["leaderboard"], 1):
            print(f"  {i}. {r['name']:<20} {r['score']}")


def cmd_ui(a):
    from .ui import serve
    serve(a.port, a.out, open_browser=not a.no_browser)


def cmd_verify(a):
    """Read-only probes plus one DRY withdraw quote. Moves no funds. Paste the output back to fix adapters."""
    for r in runner.run_probes():
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['name']}\n     {r['detail']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="contractor", description="General contractor agent for NEAR Agent Market")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("demo", cmd_demo), ("plan", cmd_plan), ("run", cmd_run), ("verify", cmd_verify), ("ui", cmd_ui),
                     ("missions", cmd_missions), ("play", cmd_play)):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if name == "ui":
            sp.add_argument("--port", type=int, default=8765)
            sp.add_argument("--no-browser", action="store_true")
            sp.add_argument("--out", default="runs")
        elif name == "play":
            sp.add_argument("--mission", default="launch-brief")
            sp.add_argument("--make", default="", help="comma-separated flexible part ids to do yourself")
            sp.add_argument("--buy", default="", help="comma-separated flexible part ids to buy")
            sp.add_argument("--stars", action="append", help="priority 1-5, e.g. --stars market_data=3 (repeatable)")
            sp.add_argument("--name", default="Player")
            sp.add_argument("--out", default="runs")
            sp.add_argument("--no-save", action="store_true", help="do not record XP or leaderboard")
        elif name == "missions":
            pass
        elif name != "verify":
            sp.add_argument("--task", default=str(runner.EXAMPLE))
            sp.add_argument("--budget", default=None)
            sp.add_argument("--out", default="runs")
        if name == "run":
            sp.add_argument("--approve", default=None, help="plan hash printed by `plan`")
            sp.add_argument("--judge", action="store_true", help="add an LLM rubric veto (needs LLM_* env)")
            sp.add_argument("--bid-window", type=float, default=300)
            sp.add_argument("--submit-window", type=float, default=3600)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
