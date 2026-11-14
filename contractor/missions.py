"""Game missions for Contractor Quest.

A mission is a task brief plus a cast of seller NPCs. The player never sees the NPC price lists, only
"intel" about each part, and decides two things before the run:

  * priority (1-5 stars) per part: more stars means a bigger share of the budget, so a higher spend cap
  * make or buy for the parts marked flex: do it yourself (cheap, lower XP) or hire an NPC (risky, higher XP)

Everything after that is the real contractor loop: caps come from code, the cheapest quote wins, one counter,
checks decide, escrow pays only for work that passes. The game adds scoring on top (see game.py) and
changes none of the money rules.
"""
from decimal import Decimal
from typing import Dict, List

from .adapters.mock import MockWorker
from .models import Part, Task
from .money import D

MIN_STARS, MAX_STARS = 1, 5


def _npc(name, price, floor, quality="good", persona=""):
    return {"name": name, "price": price, "floor": floor, "quality": quality, "persona": persona}


def _part(id, title, spec, mode="buy", stars=1, flex=False, checks=None, depends_on=None, intel="", npcs=None):
    return {"id": id, "title": title, "spec": spec, "mode": mode, "stars": stars, "flex": flex,
            "checks": checks or {"min_words": 10}, "depends_on": depends_on or [], "intel": intel, "npcs": npcs or []}


MISSIONS: List[dict] = [
    {
        "id": "launch-brief", "name": "Launch Brief", "difficulty": 1, "budget": "20", "currency": "USDC",
        "tagline": "Tutorial: split the budget, haggle, and survive one revision.",
        "brief": "A client wants a launch brief for a NEAR remittance app. Buy the research, write the rest yourself.",
        "grades": {"S": 830, "A": 720, "B": 600},
        "ghosts": [("Rusty", 430), ("Mira", 660), ("Atlas", 800)],
        "parts": [
            _part("outline", "Outline", "Write a one-page outline covering market, competitors and risks.",
                  mode="make", flex=True, intel="Easy to write yourself. Outliner Olive sells it for about 2.",
                  npcs=[_npc("Outliner Olive", 2.5, 2.0, "good", "Quick, tidy, a little pricey")]),
            _part("market_data", "Market data",
                  "Summarise remittance corridor volumes and fees for Kenya, Philippines and Mexico.",
                  stars=2, checks={"min_words": 40, "required_terms": ["Kenya", "Philippines", "Mexico"]},
                  intel="The big one. Sellers ask roughly 9 to 11 and may come down if you hold firm.",
                  npcs=[_npc("Ledger Lenny", 9.5, 8.4, "good", "Knows every corridor, drives a hard bargain"),
                        _npc("Datadog Dan", 11, 10, "good", "Premium data, premium price")]),
            _part("competitor_scan", "Competitor scan", "List five competing remittance products with pricing.",
                  stars=2, checks={"min_words": 40, "required_terms": ["pricing", "fees"]},
                  intel="Mid-priced, around 5 to 6. First drafts are often sloppy; ask for a revision.",
                  npcs=[_npc("Scout Sue", 5.5, 5.0, "revises", "Fast, but ships a rough first draft")]),
            _part("risk_review", "Risk review", "Identify regulatory and liquidity risks.",
                  stars=2, checks={"min_words": 40, "required_terms": ["regulatory", "liquidity"]},
                  intel="Cheap, about 3. Worth buying.",
                  npcs=[_npc("Riskbot", 3.4, 2.8, "good", "Never sleeps, never negotiates well")]),
            _part("final_brief", "Final brief", "Assemble the delivered parts into the final brief.",
                  mode="make", depends_on=["outline", "market_data", "competitor_scan", "risk_review"]),
        ],
    },
    {
        "id": "hard-bargain", "name": "Hard Bargain", "difficulty": 2, "budget": "30", "currency": "USDC",
        "tagline": "The sellers are greedy. You cannot afford all of them.",
        "brief": "An audit pack for a lending protocol. Chain data is expensive and the budget will not cover everything.",
        "grades": {"S": 810, "A": 720, "B": 600},
        "ghosts": [("Rusty", 420), ("Mira", 650), ("Atlas", 780)],
        "parts": [
            _part("scope", "Scope", "Write the scope of the audit pack: contracts, chains and timeline.",
                  mode="make", flex=True, intel="Simple. Do it yourself.",
                  npcs=[_npc("Scopey", 3.0, 2.5, "good", "Over-explains everything")]),
            _part("chain_data", "Chain data",
                  "Pull TVL, utilisation and liquidation history for the three largest pools.",
                  stars=2, checks={"min_words": 40, "required_terms": ["TVL", "liquidation"]},
                  intel="Rare data. Expect asks of 12 to 14, with a floor near 11 or 12.",
                  npcs=[_npc("Whale Watcher", 14, 11.5, "good", "Sits on a mountain of on-chain history")]),
            _part("tokenomics", "Tokenomics", "Review emissions, vesting and holder concentration.",
                  stars=2, checks={"min_words": 40, "required_terms": ["emissions", "vesting"]},
                  intel="Asks around 9, will drop to 7 or 8 for a fair counter.",
                  npcs=[_npc("Tokenomist Tia", 9, 7.5, "good", "Spreadsheet wizard")]),
            _part("security_notes", "Security notes", "List the top security concerns with severity.",
                  stars=2, flex=True, checks={"min_words": 8},
                  intel="Cipher Cat charges 7 to 8 for this. You could write the notes yourself instead.",
                  npcs=[_npc("Cipher Cat", 8, 7.5, "good", "Reliable, but not cheap")]),
            _part("summary", "Summary", "Combine everything into a one-page summary.",
                  mode="make", depends_on=["scope", "chain_data", "tokenomics", "security_notes"]),
        ],
    },
    {
        "id": "scam-alley", "name": "Scam Alley", "difficulty": 3, "budget": "24", "currency": "USDC",
        "tagline": "Cheap bids everywhere. Some of them are scams.",
        "brief": "A market scan for a wallet app. The cheapest bidder wins the job automatically, so choose your battles.",
        "grades": {"S": 810, "A": 720, "B": 640},
        "ghosts": [("Rusty", 400), ("Mira", 680), ("Atlas", 790)],
        "parts": [
            _part("brief", "Brief", "Write a short brief for the wallet market scan.", mode="make"),
            _part("market_scan", "Market scan", "Summarise the wallet market and its main segments.",
                  stars=2, flex=True, checks={"min_words": 8},
                  intel="WARNING: bids here are suspiciously low. The cheapest ones are known scammers.",
                  npcs=[_npc("Fast Freddie", 3.0, 2.5, "badhash", "Delivers instantly. The proof never matches."),
                        _npc("Honest Hana", 6.0, 5.5, "good", "Costs more, delivers")]),
            _part("competitor_list", "Competitor list", "List the leading wallet apps and what they charge.",
                  stars=2, flex=True, checks={"min_words": 8},
                  intel="Clean board. Reputable sellers, normal prices around 6 to 8.",
                  npcs=[_npc("Honest Hana", 6.5, 5.8, "good", "Costs more, delivers"),
                        _npc("Pricey Pete", 8, 7, "good", "Gold-plated")]),
            _part("risk_notes", "Risk notes", "Note the main regulatory and custody risks.",
                  stars=2, flex=True, checks={"min_words": 8},
                  intel="Mixed reviews. The cheap seller is rough but people say he fixes his work.",
                  npcs=[_npc("Cheap Charlie", 1.5, 1.0, "revises", "Sloppy first draft, fixes it when asked")]),
            _part("final", "Final report", "Assemble the report.", mode="make",
                  depends_on=["brief", "market_scan", "competitor_list", "risk_notes"]),
        ],
    },
    {
        "id": "rush-order", "name": "Rush Order", "difficulty": 4, "budget": "45", "currency": "USDC",
        "tagline": "Boss fight: tight money, a scammer, a slacker, and a stubborn expert.",
        "brief": "A same-day diligence pack for a cross-border payments launch. Everything is wrong at once.",
        "grades": {"S": 850, "A": 760, "B": 640},
        "ghosts": [("Rusty", 420), ("Mira", 700), ("Atlas", 820)],
        "parts": [
            _part("kickoff", "Kickoff note", "Write the kickoff note for the diligence pack.", mode="make"),
            _part("liquidity_map", "Liquidity map", "Map liquidity depth for the main stablecoin corridors.",
                  stars=3, checks={"min_words": 40, "required_terms": ["liquidity", "corridor"]},
                  intel="Specialist work. Sellers ask 15 to 18 and are slow to bend.",
                  npcs=[_npc("Depth Dora", 17, 15, "revises", "Brilliant, but her first draft is always a mess")]),
            _part("fx_rates", "FX rates", "Provide current FX spreads for six corridors.",
                  stars=2, flex=True, checks={"min_words": 8},
                  intel="WARNING: a bot is undercutting everyone here. Its deliveries never verify.",
                  npcs=[_npc("Spread Bot", 4.0, 3.5, "badhash", "Cheapest by far. Proofs never match."),
                        _npc("Forex Fiona", 9, 8, "good", "Slow to bid, good work")]),
            _part("compliance_scan", "Compliance scan", "Summarise licensing requirements per corridor.",
                  stars=2, checks={"min_words": 40, "required_terms": ["licensing", "corridor"]},
                  intel="Around 10 to 12 and she will not go below about 10.",
                  npcs=[_npc("Compliance Cleo", 12, 10, "good", "Precise and stubborn")]),
            _part("risk_model", "Risk model", "Outline the key operational and FX risks.",
                  stars=1, flex=True, checks={"min_words": 8},
                  intel="Slacker Sam bids 2. Reviews say he does nothing after the first delivery.",
                  npcs=[_npc("Slacker Sam", 2.0, 1.5, "bad", "Delivers a placeholder, ignores revisions")]),
            _part("final_pack", "Final pack", "Assemble the diligence pack.", mode="make",
                  depends_on=["kickoff", "liquidity_map", "fx_rates", "compliance_scan", "risk_model"]),
        ],
    },
]


def get_mission(mission_id: str) -> dict:
    for m in MISSIONS:
        if m["id"] == mission_id:
            return m
    raise KeyError(f"unknown mission: {mission_id}")


def default_draft(mission: dict) -> Dict[str, dict]:
    return {p["id"]: {"mode": p["mode"], "stars": p["stars"]} for p in mission["parts"]}


def clean_draft(mission: dict, draft: dict) -> Dict[str, dict]:
    """Validate the player's choices. Only flex parts may switch make/buy; stars are clamped to 1-5."""
    out = default_draft(mission)
    for p in mission["parts"]:
        d = (draft or {}).get(p["id"]) or {}
        if p["flex"] and d.get("mode") in ("make", "buy"):
            out[p["id"]]["mode"] = d["mode"]
        if "stars" in d:
            try:
                out[p["id"]]["stars"] = max(MIN_STARS, min(MAX_STARS, int(d["stars"])))
            except (TypeError, ValueError):
                pass
    return out


def build_task(mission: dict, draft: dict = None) -> Task:
    d = clean_draft(mission, draft or {})
    parts = [Part(id=p["id"], title=p["title"], spec=p["spec"], mode=d[p["id"]]["mode"],
                  weight=D(d[p["id"]]["stars"]), checks=dict(p["checks"]), depends_on=list(p["depends_on"]))
             for p in mission["parts"]]
    return Task(mission["name"], mission["brief"], D(mission["budget"]), mission["currency"], parts)


def build_scenarios(mission: dict) -> Dict[str, List[MockWorker]]:
    return {p["id"]: [MockWorker.fixed(n["name"], n["price"], n["floor"], n["quality"], n["persona"])
                      for n in p["npcs"]] for p in mission["parts"] if p["npcs"]}


def personas(mission: dict) -> Dict[str, str]:
    return {n["name"]: n["persona"] for p in mission["parts"] for n in p["npcs"]}


def public_view(mission: dict) -> dict:
    """What the player may see: no NPC price lists, only intel."""
    return {"id": mission["id"], "name": mission["name"], "difficulty": mission["difficulty"],
            "budget": mission["budget"], "currency": mission["currency"], "tagline": mission["tagline"],
            "brief": mission["brief"], "grades": mission["grades"],
            "parts": [{"id": p["id"], "title": p["title"], "mode": p["mode"], "stars": p["stars"],
                       "flex": p["flex"], "intel": p["intel"]} for p in mission["parts"]]}
