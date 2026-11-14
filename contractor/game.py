"""Contractor Quest: scoring, ranks, badges and a local profile on top of the contractor loop.

The game reads receipts; it never touches money. Scores are a pure function of the receipts, the plan and the
ledger totals, so the same run always scores the same. Profile and leaderboard are a local JSON file
(runs/profile.json). There is no server, no token and no on-chain score. Ghost rivals on the leaderboard are
fixed pre-recorded scores, not other players.
"""
import json
import os
import re
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from .money import D

RANKS = [(0, "Apprentice"), (300, "Hard Hat"), (800, "Foreman"), (1600, "Site Boss"),
         (2800, "Master Builder"), (4500, "Legend")]

POINTS = {"deal": 100, "haggle": 40, "revision": 40, "self": 50, "scam": 60, "walk": 10,
          "fail": -50, "complete": 200, "thrift": 100, "clean": 100}

BADGES = {
    "first_deal": ("First Deal", "Close a deal with a seller."),
    "haggler": ("Haggler", "Make a seller meet your counter-offer."),
    "comeback": ("Quality Control", "Get a bad delivery fixed with one revision request and pay only then."),
    "scam_caught": ("Scam Caught", "Withhold payment from a delivery that failed its check."),
    "walk_away": ("Walk-Away Power", "Walk away from a deal that was above plan."),
    "hands_on": ("Hands-On", "Do a flexible part yourself."),
    "penny_pincher": ("Penny Pincher", "Finish every part with at least 20% of the budget unspent."),
    "flawless": ("Flawless", "Deliver every part with nothing withheld, failed or lost."),
    "s_rank": ("S-Rank", "Earn an S on any mission."),
    "all_clear": ("Site Inspector", "Earn at least a B on every mission."),
}


ACCOUNT_RE = re.compile(r"^(([a-z\d]+[\-_])*[a-z\d]+\.)*([a-z\d]+[\-_])*[a-z\d]+$")


def valid_account_id(a: str) -> bool:
    return isinstance(a, str) and 2 <= len(a) <= 64 and bool(ACCOUNT_RE.fullmatch(a))


def rank_for(xp: int) -> dict:
    idx = max(i for i, (t, _) in enumerate(RANKS) if xp >= t)
    nxt = RANKS[idx + 1] if idx + 1 < len(RANKS) else None
    return {"name": RANKS[idx][1], "floor": RANKS[idx][0], "next_name": nxt[1] if nxt else None,
            "next_at": nxt[0] if nxt else None}


def _counter_accepted(r: dict) -> bool:
    acts = [s.get("action") for s in r.get("negotiation", [])]
    return "counter" in acts and acts[-1] == "accept"


def part_points(r: dict, cap) -> dict:
    """Points for one receipt (a dict as produced by Receipt.to_dict()): a label list and a total."""
    lines = []
    status, cap = r["status"], D(cap)
    if status == "paid":
        lines.append(("Deal closed", POINTS["deal"]))
        if _counter_accepted(r):
            lines.append(("Haggled the price down", POINTS["haggle"]))
        if any("revision" in n for n in r.get("notes", [])):
            lines.append(("Revision got it fixed", POINTS["revision"]))
    elif status == "self_done":
        lines.append(("Built it yourself", POINTS["self"]))
    elif status == "withheld":
        lines.append(("Scam caught: payment withheld", POINTS["scam"]))
    elif status == "no_deal":
        lines.append(("Walked away from a bad deal", POINTS["walk"]))
    elif status == "failed_check":
        lines.append(("Your own draft failed its check", 0))
    else:
        lines.append(("Something went wrong", POINTS["fail"]))
    return {"part_id": r["part_id"], "title": r["title"], "lines": [{"label": a, "points": b} for a, b in lines],
            "total": sum(b for _, b in lines)}


def score_run(receipts: List[dict], plan, budget, remaining, spent, held, mission: Optional[dict] = None) -> dict:
    """Full score for a finished run. receipts are dicts; plan is a Plan."""
    caps = {p.id: p.cap for p in plan.parts}
    parts = [part_points(r, caps.get(r["part_id"], 0)) for r in receipts]
    n = max(1, len(plan.parts))
    delivered = sum(1 for r in receipts if r["status"] in ("paid", "self_done"))
    budget, remaining = D(budget), D(remaining)
    bonus = []
    comp = int(POINTS["complete"] * delivered / n)
    bonus.append({"label": f"Parts delivered: {delivered} of {n}", "points": comp})
    unspent_ratio = float(remaining / budget) if budget > 0 else 0.0
    thrift = int(POINTS["thrift"] * min(unspent_ratio, 0.4) / 0.4 * delivered / n)
    bonus.append({"label": f"Budget left unspent: {unspent_ratio * 100:.0f}%", "points": thrift})
    bad = [r for r in receipts if r["status"] not in ("paid", "self_done")]
    clean = delivered == n and not bad
    if clean:
        bonus.append({"label": "Clean sheet: every part delivered", "points": POINTS["clean"]})
    total = sum(p["total"] for p in parts) + sum(b["points"] for b in bonus)
    grade = grade_for(total, mission["grades"]) if mission else None
    return {"parts": parts, "bonus": bonus, "total": total, "grade": grade, "delivered": delivered, "parts_total": n,
            "unspent_ratio": round(unspent_ratio, 4), "clean": clean,
            "spent": str(spent), "held": str(held), "remaining": str(remaining), "budget": str(budget)}


def grade_for(total: int, grades: dict) -> str:
    for g in ("S", "A", "B"):
        if total >= grades[g]:
            return g
    return "C" if total >= 250 else "D"


def earned_badges(result: dict, receipts: List[dict], draft_makes_flex: bool) -> List[str]:
    got = []
    st = [r["status"] for r in receipts]
    if "paid" in st:
        got.append("first_deal")
    if any(_counter_accepted(r) and r["status"] == "paid" for r in receipts):
        got.append("haggler")
    if any(r["status"] == "paid" and any("revision" in n for n in r.get("notes", [])) for r in receipts):
        got.append("comeback")
    if "withheld" in st:
        got.append("scam_caught")
    if "no_deal" in st:
        got.append("walk_away")
    if draft_makes_flex:
        got.append("hands_on")
    if result["delivered"] == result["parts_total"] and result["unspent_ratio"] >= 0.20:
        got.append("penny_pincher")
    if result["clean"]:
        got.append("flawless")
    if result["grade"] == "S":
        got.append("s_rank")
    return got


class Profile:
    """Local save file. Atomic writes; corrupt or missing file starts a fresh profile."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"name": "Player", "xp": 0, "best": {}, "badges": [], "board": {}, "plays": 0, "wallet": None}
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in self.data if k in loaded})
        except (OSError, ValueError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".profile-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def set_wallet(self, account_id):
        """Link (or unlink with None) a NEAR account id as the player identity. Identity only: no keys, no funds,
        and the id is not verified by a signature, so it is a label on a local leaderboard, not proof of ownership."""
        if account_id is None or account_id == "":
            self.data["wallet"] = None
        else:
            a = str(account_id).strip().lower()
            if not valid_account_id(a):
                raise ValueError("not a valid NEAR account id")
            self.data["wallet"] = a
            self.data["name"] = a[:32]
        self.save()

    def reset(self):
        self.data = {"name": self.data.get("name", "Player"), "xp": 0, "best": {}, "badges": [], "board": {}, "plays": 0,
                     "wallet": self.data.get("wallet")}
        self.save()

    def summary(self) -> dict:
        d = self.data
        return {"name": d["name"], "wallet": d.get("wallet"), "xp": d["xp"], "rank": rank_for(d["xp"]), "best": d["best"],
                "badges": [{"id": b, "name": BADGES[b][0], "desc": BADGES[b][1]} for b in d["badges"] if b in BADGES],
                "all_badges": [{"id": k, "name": v[0], "desc": v[1]} for k, v in BADGES.items()],
                "plays": d["plays"]}

    def leaderboard(self, mission: dict) -> List[dict]:
        rows = [{"name": f"{n} (ghost)", "score": s, "ghost": True} for n, s in mission["ghosts"]]
        rows += [{"name": e["name"], "score": e["score"], "grade": e.get("grade"), "ghost": False}
                 for e in self.data["board"].get(mission["id"], [])]
        rows.sort(key=lambda r: -r["score"])
        return rows[:8]

    def apply(self, mission: dict, result: dict, receipts: List[dict], name: str, flex_made: bool) -> dict:
        """Record a finished simulated mission. Returns what changed, for the report screen."""
        d = self.data
        name = d["wallet"] if d.get("wallet") else ((name or "Player").strip()[:16] or "Player")
        d["name"] = name
        before = rank_for(d["xp"])
        prev = d["best"].get(mission["id"], {}).get("score", 0)
        total = result["total"]
        # First clear pays in full; replays pay for improvement plus 10% so grinding the same run stays small.
        gain = total if prev == 0 else max(0, total - prev) + int(0.1 * total)
        d["xp"] += gain
        d["plays"] += 1
        if total > prev:
            d["best"][mission["id"]] = {"score": total, "grade": result["grade"]}
        board = d["board"].setdefault(mission["id"], [])
        board.append({"name": name, "score": total, "grade": result["grade"], "ts": int(time.time())})
        board.sort(key=lambda e: -e["score"])
        del board[5:]
        new = [b for b in earned_badges(result, receipts, flex_made) if b not in d["badges"]]
        d["badges"] += new
        from .missions import MISSIONS
        if "all_clear" not in d["badges"] and all(
                d["best"].get(m["id"], {}).get("grade") in ("S", "A", "B") for m in MISSIONS):
            d["badges"].append("all_clear")
            new.append("all_clear")
        self.save()
        after = rank_for(d["xp"])
        return {"xp_gain": gain, "xp_total": d["xp"], "rank_before": before["name"], "rank_after": after["name"],
                "ranked_up": after["name"] != before["name"], "personal_best": total > prev,
                "new_badges": [{"id": b, "name": BADGES[b][0], "desc": BADGES[b][1]} for b in new],
                "leaderboard": self.leaderboard(mission), "profile": self.summary()}


def share_text(mission: dict, result: dict, rank_name: str) -> str:
    return (f"I just earned {result['grade']} rank ({result['total']} pts) on \"{mission['name']}\" in Contractor Quest, "
            f"an AI agent that hires seller agents, haggles and only pays for work that passes. "
            f"Now a {rank_name}. Built at NEAR HACKATHON @NEARProtocol #NEARHackathon")
