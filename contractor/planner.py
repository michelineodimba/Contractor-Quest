"""Planning. Parts, make-or-buy and weights may come from a human or an LLM; the money never does.

Caps are computed here, in code, from weights. An LLM that returns a 'cap' or 'price' field is ignored.
"""
import hashlib
import json
from decimal import Decimal
from typing import List, Optional

from .models import Part, Plan, Task
from .money import D, down, fmt


class PlanError(ValueError):
    pass


def task_from_json(obj: dict, budget=None, currency=None) -> Task:
    parts = [Part(id=p["id"], title=p.get("title", p["id"]), spec=p.get("spec", ""),
                  mode=p.get("mode", "buy"), weight=D(p.get("weight", 1)),
                  checks=p.get("checks", {}), depends_on=p.get("depends_on", []))
             for p in obj.get("parts", [])]
    return Task(title=obj["title"], description=obj.get("description", ""),
                budget=D(budget if budget is not None else obj["budget"]),
                currency=currency or obj.get("currency", "USDC"), parts=parts)


def build_plan(task: Task, reserve_pct: Decimal = Decimal("0.10")) -> Plan:
    if not task.parts:
        raise PlanError("task has no parts")
    ids = [p.id for p in task.parts]
    if len(set(ids)) != len(ids):
        raise PlanError("part ids must be unique")
    for p in task.parts:
        if p.mode not in ("make", "buy"):
            raise PlanError(f"part {p.id}: mode must be 'make' or 'buy'")
        if p.weight <= 0:
            raise PlanError(f"part {p.id}: weight must be positive")
        for d in p.depends_on:
            if d not in ids or ids.index(d) >= ids.index(p.id):
                raise PlanError(f"part {p.id}: depends_on {d} must be an earlier part")
    reserve = down(task.budget * D(reserve_pct))
    pool = task.budget - reserve
    buys = [p for p in task.parts if p.mode == "buy"]
    total_w = sum((p.weight for p in buys), Decimal(0))
    parts = []
    for p in task.parts:
        cap = down(pool * p.weight / total_w) if p.mode == "buy" else Decimal(0)
        parts.append(Part(**{**p.__dict__, "cap": cap}))
    plan = Plan(task.title, task.budget, task.currency, reserve, parts)
    validate_plan(plan)
    return plan


def validate_plan(plan: Plan) -> None:
    if plan.total_caps() + plan.reserve > plan.budget:
        raise PlanError("caps plus reserve exceed the budget")
    if any(p.cap < 0 for p in plan.parts):
        raise PlanError("negative cap")


def plan_hash(plan: Plan) -> str:
    canon = json.dumps({"budget": str(plan.budget), "currency": plan.currency,
                        "reserve": str(plan.reserve),
                        "parts": [[p.id, p.mode, str(p.cap), p.checks] for p in plan.parts]},
                       sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def describe_plan(plan: Plan) -> str:
    lines = [f"Plan for: {plan.task_title}",
             f"Budget {fmt(plan.budget, plan.currency)} = caps {fmt(plan.total_caps(), plan.currency)} "
             f"+ reserve {fmt(plan.reserve, plan.currency)}"]
    for p in plan.parts:
        what = "do itself" if p.mode == "make" else f"buy, cap {fmt(p.cap, plan.currency)} all-in"
        lines.append(f"  - {p.id}: {p.title} [{what}]")
    lines.append(f"Plan hash: {plan_hash(plan)}")
    return "\n".join(lines)


class LLMDecomposer:
    """Ask an LLM to split a task into parts. It may choose parts, make/buy and weights; nothing about money."""

    SYSTEM = ("You split a task into 2-6 parts. Reply with JSON only: "
              '{"parts":[{"id":"snake_case","title":"...","spec":"...","mode":"make|buy","weight":1,'
              '"checks":{"min_words":50,"required_terms":["..."]}}]}. '
              "Never include prices, caps or budgets.")

    def __init__(self, llm):
        self.llm = llm

    def decompose(self, title: str, description: str, budget, currency: str) -> Task:
        raw = self.llm.complete(self.SYSTEM, f"Task: {title}\n{description}")
        try:
            obj = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            allowed = {"id", "title", "spec", "mode", "weight", "checks", "depends_on"}
            obj["parts"] = [{k: v for k, v in p.items() if k in allowed} for p in obj["parts"]]
        except (ValueError, KeyError, TypeError) as e:
            raise PlanError(f"LLM did not return a usable plan: {e}")
        obj.update({"title": title, "description": description})
        return task_from_json(obj, budget=budget, currency=currency)
