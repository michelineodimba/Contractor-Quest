# NEAR Contractor: a general contractor agent for the NEAR AI Agent Market

Track: **Agent Economy (`agents`)**. You give it a task and a budget. It splits the task into parts, does some
itself, buys the rest from other agents on [market.near.ai](https://market.near.ai), checks the work, pays out
through escrow, and writes a receipt per part. Total spend can never exceed the budget.

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

## AI assistance disclosure

This project was written with Claude (Anthropic) generating the code, with no third-party libraries beyond
the Python standard library.

## Layout

```
contractor/ledger.py      budget ledger (hard ceiling)       contractor/negotiation.py  accept / counter once / walk away
contractor/policy.py      Treasury: the only way to pay      contractor/checker.py      acceptance checks
contractor/planner.py     parts, caps, plan hash             contractor/contractor.py   the task loop
contractor/receipts.py    per-task receipts                  contractor/adapters/       mock, market.near.ai, Agent API
```
