# Contractor Quest: a game about running a NEAR agent contractor

Tracks: **Games With Real Economies (`games`)**, **Agent Economy (`agents`)** and **Open Innovation (`open`)**.

Agent Economy fit: the agent has its own wallet and budget, hires other agents on market.near.ai, negotiates, checks the work and pays through escrow.

You play a contractor with a fixed budget. Each mission is a job split into parts. You decide how much priority each
part gets (1 to 5 stars, which sets its spend cap) and which parts you write yourself. Then the agent hires seller
NPCs, haggles once, checks the work and pays only for what passes. You score points, earn XP, ranks and badges, and
land on a local leaderboard. Money rules are enforced in code, so spend can never exceed the budget.

It is a game on top of **NEAR Contractor**, an agent that buys work from other agents on
[market.near.ai](https://market.near.ai) and pays through a NEAR Intents Agent API wallet. The game uses the same
loop (ledger, treasury, negotiation, checks, receipts) against simulated sellers.

## Try it (Python 3.10+, no dependencies)

```bash
python3 -m contractor ui                 # open http://127.0.0.1:8765, pick a mission, press Start
python3 -m contractor missions           # list missions and the intel you get
python3 -m contractor play --mission scam-alley --make market_scan --stars risk_notes=5
python3 -m unittest discover -s tests    # 65 tests
```

Do X, see Y:
- Start **Launch Brief** with the defaults: market data cannot be afforded, the part is lost, grade C. Give it 3 stars: grade S.
- In **Scam Alley**, buy everything: the cheapest bidder (a scammer) wins, fails the hash check and is not paid (`withheld`, dispute opened). Choose "Make it" on the warned part instead.
- Every run writes `receipts.md` and `ledger.jsonl` under `runs/`; XP and leaderboard are in `runs/profile.json`.

## The 2D job site

The UI is a 2D game scene (HTML canvas, no assets or libraries). Each part is a stall on the job site: dashed stalls are
flexible. Before you start, click a stall to change its priority stars, click its MAKE/BUY badge to switch, and hover for the
intel. A warning triangle marks a suspicious board. When the mission starts your contractor walks to each stall, the seller
NPC states a price, you counter, coins fly from the wallet to escrow, the package arrives, and the stall turns green (paid) or
red (scam caught). The scene replays the real receipts from the run, so what you see is what the ledger recorded. "Skip
animation" speeds it up, and the receipt log below shows every detail.

## The game

| Mission | Lesson |
|---|---|
| 1. Launch Brief | Split a budget; one haggle; one revision |
| 2. Hard Bargain | You cannot afford everyone; make one part yourself |
| 3. Scam Alley | The cheapest bid wins automatically; avoid scam boards |
| 4. Rush Order | Everything at once |

Player choices: priority stars per part, and make-or-buy for flexible parts. Sellers' price lists are hidden; you only see
intel (some of it misleading by omission, for example a rough seller who does fix his work).
Scoring (`contractor/game.py`, a pure function of the receipts): deal closed +100, haggled +40, revision fixed +40,
built it yourself +50, scam caught +60, walked away +10, error -50, plus parts delivered (up to 200), unspent budget (up to 100)
and a clean-sheet bonus (100). Grades S/A/B/C/D per mission. XP: first clear pays in full; replays pay only
the improvement plus 10%. Ranks: Apprentice, Hard Hat, Foreman, Site Boss, Master Builder, Legend. 10 badges.

## Connect a NEAR wallet (identity only)

The **Connect wallet** button in the top bar links a NEAR account as your player identity (shown on the leaderboard).
Two ways: **Connect Meteor Wallet** (loads `@near-wallet-selector/meteor-wallet` 9.5.4 from esm.sh in your browser, mainnet,
so it needs internet) or type your account id. It never moves funds, never asks for a seed phrase, and the game sends no
transaction. The typed id is validated for format only and the id is not proven by a signature, so treat it as a label.
The Meteor sign-in asks Meteor for a limited access key for `social.near` (wallet-selector requires a contract id); it
cannot move funds. **The Meteor button was not tested against a real wallet** (the build sandbox has no internet); if it
fails, the typed id works and the error is shown in the dialog. Spending still goes only through the Agent API wallet.

## What is real and what is simulated

- **Real code, tested:** ledger, treasury and spend policy, negotiation, checks, receipts, plan-hash approval, both HTTP adapters (with a fake transport in tests), scoring, profile.
- **Simulated:** all game missions run against a mock market and mock wallet (`adapters/mock.py`), with scripted seller NPCs. No NEAR network call is made in game missions.
- **Not run against live services:** the market and Agent API adapters (see "Unverified assumptions" below). Live mode is under "Advanced" in the UI and needs your own keys and a typed plan hash. Live and custom runs are practice only: no XP, no leaderboard.
- **Local only:** XP, badges and leaderboard live in a JSON file on your computer. Ghost rivals are fixed pre-recorded scores, not players. Nothing is on-chain.

## Hackathon notes

- Baseline (before gamification): the original NEAR Contractor, files other than those below. New in this submission: `contractor/missions.py`, `contractor/game.py`, the new `contractor/ui.py` page and endpoints, the `missions` and `play` CLI commands, seller price fields in `adapters/mock.py`, `runner.make_mission`, and `tests/test_game.py`. Baseline commit: 7654310 (original NEAR Contractor engine, uploaded 13 Nov). The game layer was added afterwards, in the files listed above; the project also moved from a subfolder to the repo root, so use the file list above rather than the raw diff.
- The NEAR integration (market.near.ai, NEAR Intents Agent API wallet) is in the baseline and unchanged by the game layer.

## Original README: NEAR Contractor (the engine under the game)

## Easiest way: the browser UI (no network, no funds)

```bash
python3 -m contractor ui
```

Python 3.10+ is all you need. This opens a page at `http://127.0.0.1:8765` (your own computer only):

1. **Your task:** a sample task and a budget are filled in. Edit them if you like.
2. **Preview plan:** shows how the budget is split between parts the agent does itself and parts it buys.
3. **Run simulated demo:** runs the whole loop against a fake market and wallet, step by step.

While it runs you see the **agent wallet** (balance, the rules it can't change, each payment), a **budget ring**
(spent / held / unspent), and a **receipt card per part** with the negotiation timeline (quote above plan,
one counter, accepted or walked away), the check result and the escrow payments.

**Live mode** (a collapsed section) has a form for the five connection values, kept in memory only and never
written to disk, a read-only **Check connection** button (it also does one dry wallet quote and moves no funds),
and a run button that stays locked until you type the plan hash.

Files are also saved in `runs/<id>/` (`receipts.md`, `receipts.jsonl`, `ledger.jsonl`, `result.md`).
The demo shows: a bid above plan that gets one counter and is then accepted, a part that fails its check, is
revised once and then passes, and a part that still fails and is withheld (payment not released, dispute opened).

The command line does the same: `python3 -m contractor demo`. Tests: `python3 -m unittest discover -s tests`.

## How it keeps the budget safe

1. **Caps come from code, not from an LLM.** `planner.build_plan` turns part weights into per-part all-in caps
   (budget minus a 10% reserve). An LLM may propose parts, make-or-buy and weights; any price or cap it returns is ignored.
2. **Ledger.** `reserved + spent <= budget` is asserted on every change.
3. **Treasury is the only code that can pay.** It checks the recipient allowlist, currency, a per-payment cap and
   the part's reservation, runs a `dry: true` preflight on the wallet, then sends with a deterministic idempotency key.
4. **Owner-signed wallet policy is the outer wall.** Funds live in a NEAR Intents Agent API wallet whose rules
   (assets, destinations, limits, USD budget) the owner signs. The agent holds only a partner API key and a grant
   token, which cannot change those rules. Fund the wallet with exactly the task budget.
5. **Negotiation.** Quotes at or under the part's ceiling are accepted. If every quote is above it, the agent counters
   once, with reasoning, at the ceiling, then accepts only if the seller meets it, or walks away. It never accepts above the
   ceiling, and it never reveals the total budget.
6. **Checks.** Deterministic checks (length, required terms, JSON keys, hash proof) decide. An optional LLM rubric
   judge can only veto, never approve on its own, and it fails closed.
7. **Live spending needs your explicit approval.** `run` prints a plan hash and refuses to start unless you pass
   `--approve <hash>` (or type it). Changing the budget or plan changes the hash.

## Live use

```bash
cp .env.example .env     # fill in; never commit it
python3 -m contractor verify      # read-only probes + one DRY quote; moves no funds
python3 -m contractor plan --task examples/launch_brief.json --budget 5
python3 -m contractor run  --task examples/launch_brief.json --budget 5 --approve <hash from plan>
```

Setup you must do yourself (the agent cannot): register on the market and get an API key; create an Agent API
key in the partner dashboard; sign `agent_create` (policy: withdraw only, USDC, destinations = the market deposit
address plus your own refund address, per-transaction cap, USD budget) and `grant_issue`; fund the agent wallet.
Intents has no testnet, so use tiny mainnet amounts.

## Unverified assumptions (please read)

The sandbox this was written in had no network, and I could not read the full API specs. These are the places most
likely to need a fix; each is isolated in one adapter and marked `VERIFY` in the code. `python3 -m contractor verify`
prints the raw responses so you can adjust them quickly.

| Area | Assumption | Where |
|---|---|---|
| Market | Job creation body (`budget_amount`, `budget_token`, `tags`), bid list path and fields, job message endpoint, dispute endpoint, where the submission appears on the job | `adapters/market_near.py` |
| Market | There is no native counter-offer endpoint; the counter is sent as a job message and a revised bid is awaited | `NearMarket.counter` |
| Market | Escrow on award is drawn from the requester's market wallet, which the Agent API wallet tops up by `withdraw` to `MARKET_DEPOSIT_ADDRESS`; the fee (~2%, from public job records) is paid on top | `contractor.py`, `RunConfig.fee_rate` |
| Agent API | Withdraw body field names and atomic units; the success status string (unknown statuses fail closed); USDC asset id and decimals | `adapters/agent_wallet.py`, `config.py` |
| Not built | Paying HTTP 402 (x402) data paywalls via PingPay, and cross-currency payments via NEAR Intents swaps: one currency per run for now | |

## What is simulated vs real

The demo, the counter-offer behaviour of sellers, and the `TemplateExecutor` (a stand-in for "do it myself") are
simulated. The ledger, treasury, negotiation, checks, receipts, plan approval and both HTTP adapters are real code,
tested with an injected fake transport, but the adapters have **not** been run against the live services.

## Where NEAR fits

• market.near.ai (Agent Market): the engine hires seller agents there. It collects bids, counters once, awards the job, and releases escrow only after the delivery passes its checks.
• NEAR Intents Agent API wallet: the agent pays from a wallet whose spend policy (allowed asset, destinations, per-payment cap, total budget) is signed by the owner. The agent holds only an API key and a grant token, which cannot change those rules.
• NEAR account (identity only): the Connect wallet button links a NEAR account (Meteor Wallet or a typed id) as the player name on the leaderboard. It never moves funds.
• Status: game missions use a mock market and mock wallet (contractor/adapters/mock.py), so no NEAR network call is made. The live adapters (adapters/market_near.py, adapters/agent_wallet.py) are implemented and tested with a fake transport, but have not been run against live services.

## AI assistance disclosure

This project, including the game layer, was written with Claude (Anthropic) generating the code, with no third-party libraries beyond
the Python standard library.

## Layout

```
contractor/missions.py    game missions and seller NPCs        contractor/game.py         scoring, ranks, badges, profile
contractor/ledger.py      budget ledger (hard ceiling)       contractor/negotiation.py  accept / counter once / walk away
contractor/policy.py      Treasury: the only way to pay      contractor/checker.py      acceptance checks
contractor/planner.py     parts, caps, plan hash             contractor/contractor.py   the task loop
contractor/receipts.py    per-task receipts                  contractor/adapters/       mock, market.near.ai, Agent API
```
