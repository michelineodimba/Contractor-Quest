"""Local web UI: `python -m contractor ui` opens http://127.0.0.1:8765 in your browser.

Binds to localhost only, requires a per-session token on every API call, and checks the Host header
(so other web pages cannot drive it). Live spending still needs the plan hash typed in by you.
"""
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import game, missions, runner
from .money import fmt
from .planner import build_plan, plan_hash


CONNECT_KEYS = ["NEAR_MARKET_API_KEY", "MARKET_DEPOSIT_ADDRESS", "NEAR_INTENTS_AGENT_API_KEY",
                "AGENT_ID", "AGENT_GRANT_TOKEN"]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.mode = None
        self.error = None
        self.plan = None
        self.contractor = None
        self.result = None
        self.done = False
        self.mission = None
        self.game = None
        self.profile = None


def plan_dict(plan):
    return {"title": plan.task_title, "budget": str(plan.budget), "currency": plan.currency,
            "reserve": str(plan.reserve), "hash": plan_hash(plan),
            "parts": [{"id": p.id, "title": p.title, "mode": p.mode, "cap": str(p.cap)} for p in plan.parts]}


def snapshot(st: State):
    c = st.contractor
    totals = None
    if c:
        totals = {"budget": str(c.ledger.total), "spent": str(c.ledger.total_spent),
                  "held": str(c.ledger.total_reserved), "remaining": str(c.ledger.available()),
                  "currency": c.ledger.currency}
    wallet = None
    if c:
        w = c.treasury.wallet
        wallet = {"kind": "simulated" if hasattr(w, "sent") else "live",
                  "balance": str(w.balance) if hasattr(w, "balance") else None, **c.treasury.describe()}
    receipts = [r.to_dict() for r in (c.store.receipts if c else [])]
    caps = {p["id"]: p["cap"] for p in (st.plan or {}).get("parts", [])}
    pts = [game.part_points(r, caps.get(r["part_id"], 0)) for r in receipts]
    return {"running": st.running, "done": st.done, "mode": st.mode, "error": st.error, "plan": st.plan,
            "totals": totals, "wallet": wallet, "receipts": receipts, "part_points": pts,
            "mission": st.mission, "game": st.game,
            "result": st.result, "live_missing": runner.live_env_missing(),
            "connected": {k: bool(os.environ.get(k)) for k in CONNECT_KEYS}}


def start_run(st: State, mode, task_text, budget, approve, out, pace=0, mission_id=None, draft=None, name=None):
    with st.lock:
        if st.running:
            raise RuntimeError("a run is already in progress")
        if mission_id and mode == "demo":
            missions.get_mission(mission_id)          # unknown id fails before anything starts
        st.running, st.done, st.error, st.result, st.contractor, st.plan, st.mode = True, False, None, None, None, None, mode
        st.game, st.mission = None, None

    def work():
        try:
            mission = None
            if mission_id and mode == "demo":
                mission, d, task, plan, c = runner.make_mission(mission_id, draft, out, pace=pace)
                st.mission = {"id": mission["id"], "name": mission["name"], "personas": missions.personas(mission)}
            else:
                task = runner.parse_task(task_text, budget)
                if mode == "live":
                    plan, c = runner.make_live(task, out, approve)
                else:
                    plan, c = runner.make_demo(task, out, pace=pace)
            st.plan, st.contractor = plan_dict(plan), c
            rep = c.run(task, plan)
            st.result = Path(rep.result_path).read_text()
            if mission:
                result, receipts, flex_made = runner.score_mission(mission, d, plan, c, rep)
                applied = st.profile.apply(mission, result, receipts, name, flex_made)
                result["share"] = game.share_text(mission, result, applied["rank_after"])
                st.game = {"result": result, "applied": applied}
        except runner.ApprovalError as e:
            st.error = str(e)
        except Exception as e:
            st.error = f"{type(e).__name__}: {e}"
        finally:
            st.running, st.done = False, True

    threading.Thread(target=work, daemon=True).start()


def make_handler(st: State, token: str, allowed_hosts: set, out: str):

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _ok(self):
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, json.dumps({"error": "bad host"}))
                return False
            return True

        def _authed(self):
            if not self._ok():
                return False
            if self.headers.get("X-UI-Token") != token:
                self._send(403, json.dumps({"error": "bad token"}))
                return False
            return True

        def do_GET(self):
            if not self._ok():
                return
            if self.path == "/":
                self._send(200, PAGE.replace("__TOKEN__", token), "text/html; charset=utf-8")
            elif self.path == "/api/example":
                if self._authed():
                    self._send(200, Path(runner.EXAMPLE).read_text())
            elif self.path == "/api/state":
                if self._authed():
                    self._send(200, json.dumps(snapshot(st)))
            elif self.path == "/api/missions":
                if self._authed():
                    ms = []
                    for m in missions.MISSIONS:
                        v = missions.public_view(m)
                        v["best"] = st.profile.data["best"].get(m["id"])
                        v["leaderboard"] = st.profile.leaderboard(m)
                        ms.append(v)
                    self._send(200, json.dumps({"missions": ms, "profile": st.profile.summary()}))
            else:
                self._send(404, "{}")

        def do_POST(self):
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/plan":
                    task = runner.parse_task(body["task"], body.get("budget"))
                    if body.get("mode") == "live":
                        task.currency = os.environ.get("CURRENCY", "USDC")
                    self._send(200, json.dumps(plan_dict(build_plan(task))))
                elif self.path == "/api/run":
                    pace = min(float(body.get("pace") or 0), 3.0)
                    start_run(st, body.get("mode", "demo"), body.get("task"), body.get("budget"),
                              body.get("approve"), out, pace, body.get("mission"), body.get("draft"),
                              body.get("name"))
                    self._send(202, json.dumps({"started": True}))
                elif self.path == "/api/wallet":
                    st.profile.set_wallet(body.get("account_id"))
                    self._send(200, json.dumps({"profile": st.profile.summary()}))
                elif self.path == "/api/reset":
                    st.profile.reset()
                    self._send(200, json.dumps({"profile": st.profile.summary()}))
                elif self.path == "/api/connect":
                    # Keys are kept in this process's memory only: never written to disk, never sent back.
                    for k in CONNECT_KEYS:
                        v = str(body.get(k) or "").strip()
                        if v and "\n" not in v and "\r" not in v:
                            os.environ[k] = v
                    self._send(200, json.dumps({"connected": {k: bool(os.environ.get(k)) for k in CONNECT_KEYS}}))
                elif self.path == "/api/verify":
                    try:
                        self._send(200, json.dumps({"results": runner.run_probes()}))
                    except Exception as e:
                        self._send(200, json.dumps({"results": [], "error": f"{type(e).__name__}: {e}"}))
                else:
                    self._send(404, "{}")
            except Exception as e:
                self._send(400, json.dumps({"error": f"{type(e).__name__}: {e}"}))

    return H


def make_server(port=8765, out="runs"):
    st, token, allowed = State(), secrets.token_urlsafe(16), set()
    st.profile = game.Profile(Path(out) / "profile.json")
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st, token, allowed, out))
    actual = srv.server_address[1]
    allowed.update({f"127.0.0.1:{actual}", f"localhost:{actual}"})
    return srv, st, token


def serve(port=8765, out="runs", open_browser=True):
    srv, _, token = make_server(port, out)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Contractor Quest running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Contractor Quest</title>
<style>
:root{--bg:#0b0f14;--bg2:#10161e;--card:#141b25;--card2:#192231;--tx:#e8edf4;--mut:#93a0b4;--bd:#243044;--ac:#2fe6a1;--ac2:#5aa9ff;--gold:#ffcc4d;--ok:#4cd694;--bad:#ff6b6b;--warn:#ffb84d;--soft:#0f151d;--r:14px}
@media (prefers-color-scheme:light){:root{--bg:#eef2f7;--bg2:#e4eaf2;--card:#fff;--card2:#f4f7fb;--tx:#101722;--mut:#576478;--bd:#d7deea;--ac:#00a56a;--ac2:#0b63c8;--gold:#b37800;--ok:#0f7b3f;--bad:#c0362c;--warn:#a96a00;--soft:#eef2f7}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--tx)}
button{font:inherit;font-weight:700;cursor:pointer;border-radius:10px;border:1px solid var(--ac);background:var(--ac);color:#03271a;padding:10px 18px}
button.sec{background:transparent;color:var(--tx);border-color:var(--bd)}button.danger{background:var(--bad);border-color:var(--bad);color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:2px solid var(--ac2);outline-offset:2px}
input,textarea{font:inherit;color:var(--tx);background:var(--soft);border:1px solid var(--bd);border-radius:8px;padding:8px 10px}
textarea{width:100%;height:190px;font:12px/1.45 ui-monospace,Consolas,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
/* HUD */
.hud{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--bd)}
.hud .wrap{display:flex;align-items:center;gap:16px;padding:10px 16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:-.3px;font-size:18px}
.logo{width:34px;height:34px;border-radius:9px;background:var(--ac);color:#03271a;display:grid;place-items:center;font-weight:900}
.hudr{margin-left:auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.rank{font-weight:800;color:var(--gold)}.xpbar{width:170px;height:10px;background:var(--soft);border:1px solid var(--bd);border-radius:99px;overflow:hidden}
.xpbar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--ac),var(--ac2));transition:width .9s cubic-bezier(.2,.8,.2,1)}
.xpt{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
#pname{width:120px;padding:6px 8px}
.modechip{font-size:11px;font-weight:800;letter-spacing:.5px;padding:3px 10px;border-radius:99px;border:1px solid var(--warn);color:var(--warn)}
.modechip.live{border-color:var(--bad);color:var(--bad)}
/* hero */
.hero{padding:30px 0 6px}.hero h1{margin:0;font-size:30px;letter-spacing:-.6px}.hero p{margin:6px 0 0;color:var(--mut);max-width:720px}
.how{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.how span{font-size:13px;background:var(--card);border:1px solid var(--bd);border-radius:99px;padding:5px 12px;color:var(--mut)}.how b{color:var(--ac)}
h2{font-size:13px;letter-spacing:1.2px;text-transform:uppercase;color:var(--mut);margin:28px 0 10px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:18px}
/* missions */
.missions{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.mc{all:unset;box-sizing:border-box;cursor:pointer;background:var(--card);border:2px solid var(--bd);border-radius:var(--r);padding:14px;display:flex;flex-direction:column;gap:6px;transition:transform .12s,border-color .12s}
.mc:hover{transform:translateY(-2px)}.mc:focus-visible{outline:2px solid var(--ac2)}.mc.sel{border-color:var(--ac);background:var(--card2)}
.mc .top{display:flex;align-items:center;justify-content:space-between}.mc b{font-size:16px}.mc .tg{font-size:13px;color:var(--mut)}
.diff{letter-spacing:2px;color:var(--gold);font-size:13px}.grade{font-weight:900;width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-size:14px;background:var(--soft);border:1px solid var(--bd)}
.g-S{color:#03271a;background:var(--gold);border-color:var(--gold)}.g-A{color:#03271a;background:var(--ac);border-color:var(--ac)}.g-B{color:#fff;background:var(--ac2);border-color:var(--ac2)}.g-C,.g-D{color:var(--mut)}
/* draft */
.grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;align-items:start}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.brief{color:var(--mut);margin:0 0 12px}
.prow{display:grid;grid-template-columns:minmax(0,1.2fr) 150px 140px;gap:12px;align-items:center;padding:12px 0;border-top:1px solid var(--bd)}
@media (max-width:640px){.prow{grid-template-columns:1fr}}
.prow .t{font-weight:700}.intel{font-size:13px;color:var(--mut);margin-top:2px}.intel.warn{color:var(--warn)}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:9px;overflow:hidden}.seg button{border:0;border-radius:0;background:var(--soft);color:var(--mut);padding:6px 12px;font-size:13px}
.seg button.on{background:var(--ac);color:#03271a}.fixed{font-size:12px;color:var(--mut);border:1px dashed var(--bd);border-radius:9px;padding:5px 10px;display:inline-block}
.stars{display:flex;gap:2px}.stars button{all:unset;cursor:pointer;font-size:22px;line-height:1;color:var(--bd);padding:2px}.stars button.on{color:var(--gold)}.stars button:focus-visible{outline:2px solid var(--ac2);border-radius:4px}
.share{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums;margin-top:2px}
.go{display:flex;gap:12px;align-items:center;margin-top:14px;flex-wrap:wrap}.note{color:var(--mut);font-size:13px}.err{color:var(--bad);white-space:pre-wrap;font-size:14px;margin-top:8px}
/* arena */
.enc{border:1px solid var(--bd);border-radius:12px;padding:14px;margin-top:10px;background:var(--card2);animation:pop .35s ease-out}
@keyframes pop{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}
.eh{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.av{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;font-weight:800;background:var(--soft);border:2px solid var(--bd)}
.av.ok{border-color:var(--ok)}.av.bad{border-color:var(--bad)}.eh .nm{font-weight:800}.eh .ps{font-size:12px;color:var(--mut)}
.chip{margin-left:auto;font-size:12px;font-weight:800;padding:3px 10px;border-radius:99px;border:1px solid currentColor}
.paid,.self_done{color:var(--ok)}.withheld,.error,.failed_check,.no_deal,.unresolved{color:var(--bad)}
.kv{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0;font-size:13px}.kv span{color:var(--mut)}.kv em{font-style:normal;font-weight:700}
.tl{list-style:none;margin:8px 0 0;padding:0 0 0 14px;border-left:2px solid var(--bd)}.tl li{position:relative;margin:5px 0;padding-left:12px;font-size:13px}
.tl li:before{content:"";position:absolute;left:-19px;top:7px;width:8px;height:8px;border-radius:50%;background:var(--mut)}.tl li.good:before{background:var(--ok)}.tl li.bad:before{background:var(--bad)}.tl li.pay:before{background:var(--ac2)}
.pts{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}.pts span{font-size:12px;font-weight:800;background:var(--soft);border:1px solid var(--bd);border-radius:99px;padding:2px 10px;color:var(--ac)}.pts span.neg{color:var(--bad)}.pts span.tot{background:var(--ac);color:#03271a;border-color:var(--ac)}
/* side */
aside{display:flex;flex-direction:column;gap:14px}@media (min-width:861px){aside{position:sticky;top:70px}}
.big{font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.5px}.kvl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px;margin-top:8px}.kvl span{color:var(--mut)}.kvl b{text-align:right;word-break:break-all}
.hp{height:16px;border-radius:99px;overflow:hidden;display:flex;background:var(--soft);border:1px solid var(--bd);margin:8px 0}.hp i{display:block;height:100%;transition:width .6s}
.leg{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--mut)}.leg i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}
.pay{font-size:12px;padding:6px 8px;background:var(--soft);border-radius:8px;margin-top:6px;word-break:break-all}
.toast{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:50;pointer-events:none}
.tt{background:var(--card2);border:1px solid var(--ac);color:var(--ac);font-weight:800;padding:8px 14px;border-radius:10px;animation:tin .3s ease-out,tout .4s ease-in 2.6s forwards}
.tt.neg{border-color:var(--bad);color:var(--bad)}.tt.badge{border-color:var(--gold);color:var(--gold)}
@keyframes tin{from{opacity:0;transform:translateX(24px)}to{opacity:1;transform:none}}@keyframes tout{to{opacity:0;transform:translateY(8px)}}
/* report */
.report{border:2px solid var(--ac);border-radius:var(--r);background:var(--card);padding:22px;margin-top:16px;animation:pop .5s ease-out}
.rhead{display:flex;gap:22px;align-items:center;flex-wrap:wrap}.bigg{width:96px;height:96px;border-radius:22px;display:grid;place-items:center;font-size:58px;font-weight:900;border:2px solid var(--bd);background:var(--soft)}
.rs{font-size:34px;font-weight:800;font-variant-numeric:tabular-nums}.rank-up{margin-top:6px;color:var(--gold);font-weight:800}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:18px}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:5px 0;border-bottom:1px solid var(--bd)}td:last-child{text-align:right;font-weight:800;font-variant-numeric:tabular-nums}td.n{color:var(--bad)}
.sub{font-weight:800;margin:0 0 6px}.badges{display:flex;gap:8px;flex-wrap:wrap}.bdg{background:var(--soft);border:1px solid var(--gold);color:var(--gold);border-radius:10px;padding:6px 10px;font-size:13px}.bdg small{display:block;color:var(--mut);font-weight:400}
.lb tr.me td{color:var(--ac);font-weight:800}.lb tr.gh td{color:var(--mut);font-weight:400}
pre{white-space:pre-wrap;background:var(--soft);padding:12px;border-radius:10px;max-height:260px;overflow:auto;font-size:13px;margin:0}
.sharebox{margin-top:16px;background:var(--soft);border:1px solid var(--bd);border-radius:10px;padding:12px;font-size:13px}
details{margin-top:28px}details>summary{cursor:pointer;font-weight:700;padding:8px 0;color:var(--mut)}.field{margin-top:8px}.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:3px}.field input{width:100%}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}.pr{font-size:13px;padding:6px 0;border-bottom:1px solid var(--bd);word-break:break-word}.ok{color:var(--ok);font-weight:700}.no{color:var(--bad);font-weight:700}
footer{margin:40px 0 30px;color:var(--mut);font-size:13px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="hud"><div class="wrap">
 <div class="brand"><div class="logo">N</div>Contractor Quest</div>
 <span class="modechip" id="mode">READY</span>
 <div class="hudr">
  <label class="note">Player <input id="pname" maxlength="16" value="Player" aria-label="Player name"></label>
  <button class="sec" id="btnWallet" style="padding:5px 12px;font-size:13px">Connect wallet</button>
  <div><div class="rank" id="rank">Apprentice</div><div class="xpt" id="xpt">0 XP</div></div>
  <div class="xpbar" role="progressbar" aria-label="XP to next rank"><i id="xpfill"></i></div>
 </div></div></div>

<div class="wrap">
<section class="hero">
 <h1>You are the contractor.</h1>
 <p>Get the job done on a fixed budget. Hire seller agents, haggle once, and pay only for work that passes its check. Spend can never go over budget: the wallet rules are enforced in code, not by luck.</p>
 <div class="how"><span><b>1</b> Pick a mission</span><span><b>2</b> Set priorities and choose make or buy</span><span><b>3</b> Watch the negotiations</span><span><b>4</b> Earn XP, badges and a grade</span></div>
</section>

<h2>Missions</h2><div class="missions" id="missions"></div>

<h2>Plan your job</h2>
<div class="grid"><div>
 <div class="card"><div id="brief" class="brief"></div><div id="draft"></div>
  <div class="go"><button id="btnGo">Start mission</button><label class="note"><input type="checkbox" id="pace" checked> show it step by step</label><span class="note">Simulated sellers. No network, no funds.</span></div>
  <div class="err" id="runErr"></div></div>

 <h2>Job site</h2>
 <div class="card" style="padding:10px;position:relative"><canvas id="stage" role="img" aria-label="Animated job site. The same results are listed in the receipt log below." style="width:100%;display:block;border-radius:10px;aspect-ratio:960/420"></canvas><div id="tip" class="note" style="display:none;position:absolute;width:220px;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:6px 9px;z-index:5;pointer-events:none"></div>
  <div class="row" style="margin-top:6px"><button class="sec" id="btnSkip" style="padding:4px 10px;font-size:12px">Skip animation</button><span class="note">Before you start: click a stall to change its priority, click MAKE/BUY on a dashed stall to switch.</span></div></div>
 <h2>Receipt log</h2>
 <div class="card" id="arena"><div class="note">Pick a mission and press Start. Each seller you face appears here.</div></div>
 <div id="report"></div>
</div>
<aside>
 <div class="card"><div class="note">AGENT WALLET <b id="wbadge" style="float:right">NOT STARTED</b></div><div id="wallet" class="note" style="margin-top:8px">When the mission starts, the wallet is funded with exactly the budget. The agent can spend it but cannot change the rules below.</div></div>
 <div class="card"><div class="note">BUDGET</div><div id="budgetBox" class="note" style="margin-top:6px">Starts when the mission starts.</div></div>
 <div class="card"><div class="note">BADGES <span id="bcount" style="float:right"></span></div><div class="badges" id="badges" style="margin-top:8px"></div>
  <div class="row"><button class="sec" id="btnReset" style="padding:4px 10px;font-size:12px">Reset progress</button></div></div>
</aside></div>

<details id="adv"><summary>Advanced: your own task, or live mode with real funds</summary>
 <div class="card" style="margin-top:8px">
  <div class="note">Runs here are practice: no XP, no leaderboard. Edit the task JSON, then run it against the simulated market, or against the real market and wallet.</div>
  <textarea id="task" spellcheck="false"></textarea>
  <div class="row"><label class="note">Budget <input id="budget" size="6" value="20"></label><button class="sec" id="btnDemo">Run task in simulation</button></div>
  <details style="margin-top:12px"><summary>Live mode: connect to the real market and wallet</summary>
   <div class="note">Keys are kept in this program's memory only. They are never saved to disk or shown again.</div>
   <div class="field"><label>market.near.ai API key</label><input type="password" id="cMk" autocomplete="off"></div>
   <div class="field"><label>Market deposit address (the one address the wallet may pay)</label><input type="text" id="cAddr" autocomplete="off"></div>
   <div class="field"><label>Agent API key</label><input type="password" id="cAk" autocomplete="off"></div>
   <div class="field"><label>Agent ID</label><input type="text" id="cAid" autocomplete="off"></div>
   <div class="field"><label>Agent grant token</label><input type="password" id="cGt" autocomplete="off"></div>
   <div class="row"><button class="sec" id="btnSave">Save for this session</button><button class="sec" id="btnVerify">Check connection (read-only)</button></div>
   <div id="verify"></div><div class="note" id="liveNote" style="margin-top:8px"></div>
   <div class="row"><input id="approve" size="22" placeholder="type the plan hash to approve"><button class="danger" id="btnLive">Run live (spends real funds)</button></div>
   <div class="note">It will not start unless the hash you type matches the previewed plan and budget. <button class="sec" id="btnPlan" style="padding:3px 10px;font-size:12px">Preview plan hash</button> <code id="hashOut"></code></div>
  </details><div class="err" id="advErr"></div>
  <div class="sub" style="margin-top:14px">Final result</div><pre id="result">(nothing yet)</pre></div>
</details>
<footer>Leaderboard and XP are saved only on this computer (runs/profile.json). Ghost rivals are fixed pre-recorded scores, not other players.</footer>
</div>
<dialog id="wdlg" style="border:1px solid var(--bd);border-radius:14px;background:var(--card);color:var(--tx);max-width:440px;padding:20px">
 <div class="sub">Connect a NEAR wallet</div>
 <p class="note">This only sets who you are on the leaderboard. It never asks for funds or a seed phrase, and the game never sends a transaction. Missions are simulated.</p>
 <div class="row"><button id="btnMeteor">Connect Meteor Wallet</button></div>
 <div class="note" id="wmsg" style="margin-top:6px"></div>
 <div class="field"><label>or type your NEAR account id (not verified)</label><input id="wacct" placeholder="alice.near" autocomplete="off"></div>
 <div class="row"><button class="sec" id="btnWsave">Use this account</button><button class="sec" id="btnWoff">Disconnect</button><button class="sec" id="btnWclose">Close</button></div>
</dialog>
<div class="toast" id="toast" aria-live="polite"></div>

<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);
const H={"X-UI-Token":TOKEN,"Content-Type":"application/json"};
const api=(p,b)=>fetch(p,b?{method:"POST",headers:H,body:JSON.stringify(b)}:{headers:H}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j});
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e};
const fmt=n=>(+n).toFixed(2);
let MISSIONS=[],PROFILE=null,SEL=null,DRAFT={},timer=null,seen=0,shownReport=false,runKind=null,RUNNING=false,needInit=false,finaleQueued=false;

function toast(text,cls){const t=el("div",text,"tt "+(cls||""));$("toast").appendChild(t);setTimeout(()=>t.remove(),3200)}

/* ---------- 2D job site (canvas) ---------- */
class Scene{
 constructor(cv){this.cv=cv;this.ctx=cv.getContext("2d");this.W=960;this.H=420;this.stalls=[];this.mode="preview";
  this.base=matchMedia("(prefers-reduced-motion: reduce)").matches?25:1;this.speed=this.base;this.queue=Promise.resolve();this.pending=0;
  this.me={x:150,y:352,walking:false,work:0,jump:0};this.bubbles=[];this.coins=[];this.floats=[];this.boxes=[];this.confetti=[];this.stamp=null;
  this.budget={total:0,spent:0,held:0};this.wallet=0;this.cur="USDC";this.hover=-1;this.last=0;this.t=0;
  this.resize();addEventListener("resize",()=>this.resize());requestAnimationFrame(f=>this.loop(f));
  cv.addEventListener("mousemove",e=>this.onMove(e));cv.addEventListener("mouseleave",()=>{this.hover=-1;$("tip").style.display="none"});cv.addEventListener("click",e=>this.onClick(e))}
 resize(){const w=this.cv.clientWidth||960,d=window.devicePixelRatio||1;this.cv.width=Math.round(w*d);this.cv.height=Math.round(w*d*this.H/this.W);this.k=this.cv.width/this.W}
 /* ---- data ---- */
 layout(){const n=this.stalls.length||1,left=250,right=930,step=(right-left)/n;this.stalls.forEach((s,i)=>{s.x=left+step*(i+.5);s.w=Math.min(104,step-12)})}
 preview(m,draft,caps){if(this.pending)return;this.mode="preview";this.cur=m.currency;this.budget={total:+m.budget,spent:0,held:0};this.wallet=+m.budget;
  this.stalls=m.parts.map(p=>({id:p.id,title:p.title,mode:draft[p.id].mode,stars:draft[p.id].stars,flex:p.flex,intel:p.intel,warn:/WARNING|Mixed|Reviews say/.test(p.intel),cap:caps[p.id]||0,npc:null,state:"idle",flash:0,fade:1}));
  this.layout();this.me.x=150;this.stamp=null;this.confetti=[];this.bubbles=[];this.floats=[]}
 start(plan){this.mode="run";this.cur=plan.currency;const old=Object.fromEntries(this.stalls.map(s=>[s.id,s]));
  this.stalls=plan.parts.map(p=>{const o=old[p.id]||{};return{id:p.id,title:p.title,mode:p.mode,stars:o.stars,flex:o.flex,intel:o.intel,warn:o.warn,cap:+p.cap,npc:null,state:"idle",flash:0,fade:1}});
  this.layout();this.budget={total:+plan.budget,spent:0,held:0};this.wallet=+plan.budget}
 async reset(){this.speed=1000;await this.queue;this.speed=this.base;this.bubbles=[];this.coins=[];this.floats=[];this.boxes=[];this.confetti=[];this.stamp=null;this.me.x=150;this.me.work=0;this.stalls.forEach(s=>{s.npc=null;s.state="idle";s.fade=1})}
 skip(){this.speed=this.speed>this.base?this.base:this.base*12;return this.speed>this.base}
 enqueue(fn){this.pending++;this.queue=this.queue.then(fn).catch(e=>console.error(e)).then(()=>{this.pending--});return this.queue}
 /* ---- primitives ---- */
 sleep(ms){return new Promise(r=>setTimeout(r,ms/this.speed))}
 tween(ms,fn){return new Promise(res=>{const st=performance.now(),tick=n=>{const k=Math.min(1,(n-st)*this.speed/ms);fn(k);k<1?requestAnimationFrame(tick):res()};requestAnimationFrame(tick)})}
 async walkTo(x){const a=this.me.x,d=Math.abs(x-a);if(d<2)return;this.me.walking=true;await this.tween(Math.max(250,d*2.4),k=>{this.me.x=a+(x-a)*(k*k*(3-2*k))});this.me.walking=false}
 async say(who,text,ms=900){const b={who,text};this.bubbles.push(b);await this.sleep(ms);this.bubbles=this.bubbles.filter(q=>q!==b)}
 async flyCoins(from,to,n,ms=900){const cs=[];for(let i=0;i<n;i++)cs.push({f:from,t:to,d:i/n*.5,k:0});this.coins.push(...cs);await this.tween(ms,k=>cs.forEach(c=>{c.k=Math.max(0,Math.min(1,(k-c.d)/(1-.5)))}));this.coins=this.coins.filter(c=>!cs.includes(c))}
 async flyBox(from,to,ms=700){const b={f:from,t:to,k:0};this.boxes.push(b);await this.tween(ms,k=>{b.k=k});this.boxes=this.boxes.filter(q=>q!==b)}
 floatText(x,y,text,color){this.floats.push({x,y,text,color,age:0})}
 spend(r){const paid=+r.total_paid||0,sent=(r.payments||[]).reduce((a,p)=>a+(+p.amount),0);this.budget.spent+=paid;this.budget.held+=Math.max(0,sent-paid);this.wallet-=sent}
 /* ---- scripted receipt playback ---- */
 async playReceipt(r,pt){const s=this.stalls.find(x=>x.id===r.part_id);if(!s)return;const fmt2=n=>(+n).toFixed(2);
  await this.walkTo(s.x);
  const pts=()=>{if(pt)this.floatText(s.x,150,(pt.total>=0?"+":"")+pt.total,pt.total<0?"bad":"ok")};
  if(r.kind==="make"){this.me.work=1;await this.sleep(1000);this.me.work=0;
   if(r.status==="self_done"){s.state="won";await this.say("me","Built it myself!",700)}else{s.state="lost";await this.say("me","My draft failed the check.",900)}pts();await this.sleep(300);return}
  const neg=r.negotiation||[];const bidder=(neg.find(n=>n.bidder)||{}).bidder||(r.seller!=="-"?r.seller:null);s.npc=bidder;
  if(!neg.length||neg[0].action==="walk_away"){await this.say(s.id,"No bids today.",800);await this.say("me","Moving on.",600);s.state="lost";pts();return}
  if(r.quoted)await this.say(s.id,"I ask "+fmt2(r.quoted)+" "+this.cur,900);
  for(const n of neg){
   if(n.action==="counter"){await this.say("me","My max is "+fmt2(n.offered)+".",900)}
   else if(n.action==="accept"){await this.say(neg.length>1?s.id:"me",neg.length>1?"Deal at "+fmt2(n.amount)+"!":"Deal: "+fmt2(n.amount),800)}
   else if(n.action==="walk_away"){await this.say(s.id,"Too low. No deal.",900);await this.say("me","Then I walk.",700);s.state="lost";await this.tween(500,k=>{s.fade=1-k});pts();return}}
  const sent=(r.payments||[]).reduce((a,p)=>a+(+p.amount),0);
  if(sent>0){await this.flyCoins("wallet",s.id,7);this.spend(r)}
  await this.sleep(250);await this.flyBox(s.id,"me");
  if((r.notes||[]).some(n=>/revision/i.test(n))){await this.say("me","This needs a fix. Please revise.",900);await this.flyBox("me",s.id);await this.sleep(300);await this.flyBox(s.id,"me")}
  if(r.status==="paid"){s.state="won";await this.say("me","Check passed. Approved!",800);pts()}
  else if(r.status==="withheld"){s.state="lost";s.flash=1;await this.say("me",((r.check.reasons||[])[0]||"Check failed")+". Payment withheld!",1300);pts()}
  else{s.state="lost";await this.say("me","Something went wrong.",900);pts()}
  await this.sleep(250)}
 async finale(grade){this.stamp={t:grade,k:0};await this.tween(500,k=>{this.stamp.k=k});
  if("SAB".includes(grade)){for(let i=0;i<90;i++)this.confetti.push({x:Math.random()*this.W,y:-10-Math.random()*80,vx:(Math.random()-.5)*80,vy:60+Math.random()*120,c:["#2fe6a1","#ffcc4d","#5aa9ff","#ff6b6b"][i%4],r:Math.random()*6});
   this.me.jump=1;await this.sleep(1700);this.me.jump=0}else await this.sleep(900)}
 /* ---- input ---- */
 pos(e){const r=this.cv.getBoundingClientRect();return[(e.clientX-r.left)*this.W/r.width,(e.clientY-r.top)*this.H/r.height]}
 hit(e){const[x,y]=this.pos(e);for(let i=0;i<this.stalls.length;i++){const s=this.stalls[i];if(Math.abs(x-s.x)<s.w/2+4&&y>90&&y<262)return[i,y<118?"badge":"body"]}return[-1,""]}
 onMove(e){const[i,z]=this.hit(e);this.hover=i;const tip=$("tip");
  if(i<0||this.mode!=="preview"){tip.style.display="none";this.cv.style.cursor="default";return}
  const s=this.stalls[i];this.cv.style.cursor="pointer";tip.textContent=s.intel||"You write this part yourself.";tip.style.display="block";
  const r=this.cv.getBoundingClientRect();tip.style.left=Math.min(r.width-230,Math.max(4,(s.x*r.width/this.W)-110))+"px";tip.style.top=(250*r.height/this.H)+"px"}
 onClick(e){if(this.mode!=="preview"||RUNNING||this.pending)return;const[i,z]=this.hit(e);if(i<0)return;const s=this.stalls[i],d=DRAFT[s.id];
  if(z==="badge"&&s.flex)d.mode=d.mode==="make"?"buy":"make";else if(d.mode==="buy")d.stars=d.stars%5+1;else return;renderDraft()}
 /* ---- drawing ---- */
 loop(f){const dt=Math.min(.05,((f-(this.last||f))/1000)||0);this.last=f;this.t+=dt;this.update(dt);this.draw();requestAnimationFrame(g=>this.loop(g))}
 update(dt){this.floats.forEach(q=>q.age+=dt);this.floats=this.floats.filter(q=>q.age<1.6);this.stalls.forEach(s=>{if(s.flash>0)s.flash=Math.max(0,s.flash-dt*.5)});
  this.confetti.forEach(c=>{c.x+=c.vx*dt;c.y+=c.vy*dt});this.confetti=this.confetti.filter(c=>c.y<this.H+20)}
 ent(w){if(w==="me")return{x:this.me.x,y:this.me.y-62-(this.me.jump?Math.abs(Math.sin(this.t*9))*22:0)};if(w==="wallet")return{x:78,y:318};const s=this.stalls.find(q=>q.id===w);return s?{x:s.x,y:s.mode==="buy"?190:200}:{x:0,y:0}}
 draw(){const c=this.ctx,css=getComputedStyle(document.documentElement),C=n=>css.getPropertyValue("--"+n).trim();c.setTransform(this.k,0,0,this.k,0,0);
  const W=this.W,H=this.H;c.clearRect(0,0,W,H);
  const g=c.createLinearGradient(0,0,0,300);g.addColorStop(0,C("bg2"));g.addColorStop(1,C("card2"));c.fillStyle=g;c.fillRect(0,0,W,H);
  c.fillStyle=C("card");c.fillRect(0,300,W,H-300);c.strokeStyle=C("bd");c.lineWidth=2;c.setLineDash([10,10]);c.beginPath();c.moveTo(0,356);c.lineTo(W,356);c.stroke();c.setLineDash([]);
  this.drawBudget(c,C);this.drawChest(c,C);this.stalls.forEach((s,i)=>this.drawStall(c,C,s,i));this.drawMe(c,C);
  this.boxes.forEach(b=>{const a=this.ent(b.f),z=this.ent(b.t),x=a.x+(z.x-a.x)*b.k,y=a.y+(z.y-a.y)*b.k-Math.sin(b.k*Math.PI)*34;c.fillStyle="#c99a5b";c.fillRect(x-9,y-9,18,18);c.strokeStyle="#8a6431";c.lineWidth=2;c.strokeRect(x-9,y-9,18,18);c.beginPath();c.moveTo(x-9,y);c.lineTo(x+9,y);c.stroke()});
  this.coins.forEach(q=>{const a=this.ent(q.f),z=this.ent(q.t),k=q.k,x=a.x+(z.x-a.x)*k,y=a.y+(z.y-a.y)*k-Math.sin(k*Math.PI)*60;if(k<=0||k>=1)return;c.fillStyle=C("gold");c.beginPath();c.arc(x,y,6,0,7);c.fill();c.strokeStyle="#0006";c.lineWidth=1;c.stroke()});
  this.bubbles.forEach(b=>this.drawBubble(c,C,b));
  this.floats.forEach(q=>{c.globalAlpha=Math.max(0,1-q.age/1.6);c.fillStyle=C(q.color);c.font="800 22px system-ui";c.textAlign="center";c.fillText(q.text,q.x,q.y-q.age*40);c.globalAlpha=1});
  this.confetti.forEach(q=>{c.fillStyle=q.c;c.fillRect(q.x,q.y,q.r,q.r*1.6)});
  if(this.stamp){const k=this.stamp.k;c.save();c.translate(W/2,150);c.rotate(-.12);c.scale(.6+.4*k,.6+.4*k);c.globalAlpha=k;c.fillStyle=C("card");c.strokeStyle=C(this.stamp.t==="S"?"gold":"ac");c.lineWidth=6;this.rr(c,-70,-70,140,140,24);c.fill();c.stroke();c.fillStyle=C(this.stamp.t==="S"?"gold":"tx");c.font="900 96px system-ui";c.textAlign="center";c.fillText(this.stamp.t,0,32);c.restore()}}
 rr(c,x,y,w,h,r){c.beginPath();c.moveTo(x+r,y);c.arcTo(x+w,y,x+w,y+h,r);c.arcTo(x+w,y+h,x,y+h,r);c.arcTo(x,y+h,x,y,r);c.arcTo(x,y,x+w,y,r);c.closePath()}
 drawBudget(c,C){const b=this.budget,x=20,y=18,w=300,h=14;c.fillStyle=C("soft");this.rr(c,x,y,w,h,7);c.fill();if(b.total>0){const sp=w*b.spent/b.total,hd=w*b.held/b.total;c.save();this.rr(c,x,y,w,h,7);c.clip();c.fillStyle=C("ac");c.fillRect(x,y,sp,h);c.fillStyle=C("warn");c.fillRect(x+sp,y,hd,h);c.restore()}
  c.strokeStyle=C("bd");c.lineWidth=1.5;this.rr(c,x,y,w,h,7);c.stroke();c.fillStyle=C("mut");c.font="600 12px system-ui";c.textAlign="left";c.fillText(`BUDGET ${b.total.toFixed(2)} ${this.cur}   spent ${b.spent.toFixed(2)}   held ${b.held.toFixed(2)}`,x,y+h+16)}
 drawChest(c,C){const x=40,y=318;c.fillStyle="#8a6431";this.rr(c,x,y,76,40,6);c.fill();c.fillStyle="#a97d3f";c.fillRect(x,y+10,76,6);c.fillStyle=C("gold");for(let i=0;i<4;i++){c.beginPath();c.arc(x+16+i*14,y+2,7,0,7);c.fill()}
  c.fillStyle=C("tx");c.font="800 13px system-ui";c.textAlign="center";c.fillText(Math.max(0,this.wallet).toFixed(2),x+38,y+62);c.fillStyle=C("mut");c.font="600 10px system-ui";c.fillText("AGENT WALLET",x+38,y+76)}
 drawStall(c,C,s,i){const x=s.x,w=s.w,top=120,bot=212;const tint=s.state==="won"?C("ok"):s.state==="lost"?C("bad"):C("bd");
  c.globalAlpha=s.fade;c.fillStyle=C("card2");this.rr(c,x-w/2,top,w,bot-top,8);c.fill();c.strokeStyle=tint;c.lineWidth=s.state==="idle"?2:4;c.stroke();
  const make=s.mode==="make";c.fillStyle=make?C("ac2"):C("ac");for(let k=0;k<5;k++){if(k%2===0){c.beginPath();c.moveTo(x-w/2+k*w/5,top);c.lineTo(x-w/2+(k+1)*w/5,top);c.lineTo(x-w/2+(k+1)*w/5-4,top-16);c.lineTo(x-w/2+k*w/5+4,top-16);c.closePath();c.fill()}}
  c.fillStyle=C("soft");c.fillRect(x-w/2+4,top-16,w-8,0);
  /* badge */ c.fillStyle=make?C("ac2"):C("ac");this.rr(c,x-22,92,44,18,9);c.fill();c.fillStyle="#03271a";c.font="800 10px system-ui";c.textAlign="center";c.fillText(make?"MAKE":"BUY",x,105);
  if(s.flex&&this.mode==="preview"){c.strokeStyle=C("tx");c.setLineDash([3,3]);c.lineWidth=1;this.rr(c,x-24,90,48,22,10);c.stroke();c.setLineDash([])}
  /* sign */ c.fillStyle=C("tx");c.font="700 11px system-ui";c.textAlign="center";const words=s.title.split(" ");let l1=words[0],l2="";for(let k=1;k<words.length;k++){if((l1+" "+words[k]).length<=13&&!l2)l1+=" "+words[k];else l2+=(l2?" ":"")+words[k]}c.fillText(l1,x,top+18);if(l2)c.fillText(l2,x,top+31);
  /* counter */ c.fillStyle=C("bd");c.fillRect(x-w/2,bot-12,w,12);
  if(make){c.fillStyle=C("mut");c.fillRect(x-16,bot-26,32,10);c.fillRect(x-8,bot-34,16,8);this.hammer(c,C,x+14,bot-34,this.me.work&&this.nearMe(s)?Math.sin(this.t*18):0)}
  else this.drawNpc(c,C,s,x,bot-12);
  /* below */ c.fillStyle=C("gold");c.font="700 12px system-ui";if(s.mode==="buy"&&s.stars)c.fillText("★".repeat(s.stars)+"☆".repeat(5-s.stars),x,236);
  c.fillStyle=C("mut");c.font="600 10px system-ui";if(!make&&s.cap>0)c.fillText("cap "+s.cap.toFixed(2),x,250);if(s.npc){c.fillStyle=C("tx");c.fillText(s.npc,x,262)}
  if(s.warn&&this.mode==="preview"){c.fillStyle=C("warn");c.beginPath();c.moveTo(x+w/2-6,top-30);c.lineTo(x+w/2+8,top-4);c.lineTo(x+w/2-20,top-4);c.closePath();c.fill();c.fillStyle="#1a1000";c.font="900 12px system-ui";c.fillText("!",x+w/2-6,top-8)}
  if(s.state==="won"){this.tick(c,x+w/2-8,top+8,C("ok"))}if(s.state==="lost"){this.cross(c,x+w/2-8,top+8,C("bad"))}
  if(s.flash>0){c.fillStyle=`rgba(255,80,80,${s.flash*.45})`;this.rr(c,x-w/2,top,w,bot-top,8);c.fill()}
  if(this.hover===i&&this.mode==="preview"){c.strokeStyle=C("ac2");c.lineWidth=2;this.rr(c,x-w/2-4,top-18,w+8,bot-top+22,10);c.stroke()}
  c.globalAlpha=1}
 nearMe(s){return Math.abs(this.me.x-s.x)<8}
 hammer(c,C,x,y,a){c.save();c.translate(x,y);c.rotate(-.8+a*.7);c.fillStyle="#8a6431";c.fillRect(-2,-18,4,18);c.fillStyle=C("mut");c.fillRect(-8,-24,16,8);c.restore()}
 tick(c,x,y,col){c.strokeStyle=col;c.lineWidth=3.5;c.lineCap="round";c.beginPath();c.moveTo(x-6,y);c.lineTo(x-1,y+5);c.lineTo(x+7,y-6);c.stroke()}
 cross(c,x,y,col){c.strokeStyle=col;c.lineWidth=3.5;c.lineCap="round";c.beginPath();c.moveTo(x-6,y-6);c.lineTo(x+6,y+6);c.moveTo(x+6,y-6);c.lineTo(x-6,y+6);c.stroke()}
 hue(n){let h=0;for(const ch of n)h=(h*31+ch.charCodeAt(0))%360;return h}
 drawNpc(c,C,s,x,y){const known=!!s.npc,col=known?`hsl(${this.hue(s.npc)},62%,58%)`:C("mut"),bob=Math.sin(this.t*2+x)*1.5;
  c.fillStyle=col;this.rr(c,x-13,y-34+bob,26,30,8);c.fill();c.fillStyle="#f1c9a0";c.beginPath();c.arc(x,y-42+bob,12,0,7);c.fill();
  c.fillStyle=col;c.beginPath();c.arc(x,y-46+bob,12,Math.PI,0);c.fill();
  if(known){c.fillStyle="#222";c.beginPath();c.arc(x-4,y-42+bob,1.8,0,7);c.arc(x+4,y-42+bob,1.8,0,7);c.fill()}else{c.fillStyle=C("card");c.font="800 13px system-ui";c.textAlign="center";c.fillText("?",x,y-38+bob)}}
 drawMe(c,C){const m=this.me,j=m.jump?Math.abs(Math.sin(this.t*9))*22:0,x=m.x,y=m.y-j,bob=m.walking?Math.sin(this.t*16)*3:Math.sin(this.t*2)*1;
  c.fillStyle="#0003";c.beginPath();c.ellipse(x,m.y+4,20,6,0,0,7);c.fill();
  c.fillStyle=C("ac2");this.rr(c,x-15,y-46+bob,30,38,9);c.fill();c.fillStyle="#1d3b6b";c.fillRect(x-12,y-10,10,10+(m.walking?Math.sin(this.t*16)*3:0));c.fillRect(x+2,y-10,10,10-(m.walking?Math.sin(this.t*16)*3:0));
  c.fillStyle="#f1c9a0";c.beginPath();c.arc(x,y-58+bob,13,0,7);c.fill();c.fillStyle=C("gold");c.beginPath();c.arc(x,y-62+bob,15,Math.PI,0);c.fill();c.fillRect(x-18,y-63+bob,36,5);
  c.fillStyle="#222";c.beginPath();c.arc(x-5,y-57+bob,1.8,0,7);c.arc(x+5,y-57+bob,1.8,0,7);c.fill();
  c.fillStyle="#fff";c.fillRect(x+13,y-34+bob,12,16);c.strokeStyle="#888";c.lineWidth=1;c.strokeRect(x+13,y-34+bob,12,16);
  if(m.work)this.hammer(c,C,x+22,y-34,Math.sin(this.t*18))}
 drawBubble(c,C,b){const e=this.ent(b.who),me=b.who==="me";c.font="600 13px system-ui";c.textAlign="center";const w=c.measureText(b.text).width+22,h=28;let x=e.x-w/2;x=Math.max(6,Math.min(this.W-w-6,x));const y=(me?e.y-20:e.y-96)-h;
  c.fillStyle=me?C("ac"):C("card");c.strokeStyle=me?C("ac"):C("tx");c.lineWidth=1.5;this.rr(c,x,y,w,h,10);c.fill();c.stroke();c.beginPath();c.moveTo(e.x-6,y+h);c.lineTo(e.x,y+h+8);c.lineTo(e.x+6,y+h);c.closePath();c.fill();
  c.fillStyle=me?"#03271a":C("tx");c.fillText(b.text,x+w/2,y+18)}
}

const scene=new Scene($("stage"));
$("btnSkip").onclick=()=>{$("btnSkip").textContent=scene.skip()?"Normal speed":"Skip animation"};
/* ---------- HUD / profile ---------- */
function renderProfile(p){PROFILE=p;$("rank").textContent=p.rank.name;
 const nx=p.rank.next_at;$("xpt").textContent=nx?`${p.xp} / ${nx} XP to ${p.rank.next_name}`:`${p.xp} XP: max rank`;
 const pct=nx?100*(p.xp-p.rank.floor)/(nx-p.rank.floor):100;$("xpfill").style.width=Math.max(2,pct)+"%";
 if(document.activeElement!==$("pname")&&p.plays)$("pname").value=p.name;
 const got=new Set(p.badges.map(b=>b.id));$("bcount").textContent=`${got.size}/${p.all_badges.length}`;const bx=$("badges");bx.replaceChildren();
 p.all_badges.forEach(b=>{const d=el("div",null,"bdg");d.style.opacity=got.has(b.id)?1:.35;d.appendChild(document.createTextNode((got.has(b.id)?"★ ":"")+b.name));d.appendChild(el("small",b.desc));bx.appendChild(d)})}

/* ---------- wallet (identity only) ---------- */
const METEOR_CORE="https://esm.sh/@near-wallet-selector/core@9.5.4",METEOR_MOD="https://esm.sh/@near-wallet-selector/meteor-wallet@9.5.4";
async function linkWallet(id){const r=await api("/api/wallet",{account_id:id});renderProfile(r.profile);$("pname").value=r.profile.name}
$("btnWallet").onclick=()=>{$("wmsg").textContent=PROFILE&&PROFILE.wallet?"Connected as "+PROFILE.wallet:"";$("wdlg").showModal()};
$("btnWclose").onclick=()=>$("wdlg").close();
$("btnWsave").onclick=async()=>{try{await linkWallet($("wacct").value);$("wmsg").textContent="Linked "+PROFILE.wallet+" (not verified)"}catch(e){$("wmsg").textContent=e.message}};
$("btnWoff").onclick=async()=>{try{await linkWallet(null);$("wmsg").textContent="Disconnected"}catch(e){$("wmsg").textContent=e.message}};
$("btnMeteor").onclick=async()=>{const m=$("wmsg");m.textContent="Loading Meteor Wallet (needs internet)...";
 try{const [core,met]=await Promise.all([import(METEOR_CORE),import(METEOR_MOD)]);
  const selector=await core.setupWalletSelector({network:"mainnet",modules:[met.setupMeteorWallet()]});
  const wallet=await selector.wallet("meteor-wallet");
  let accts=await wallet.getAccounts();
  if(!accts.length){m.textContent="Approve the sign-in in the Meteor window...";
   accts=await wallet.signIn({contractId:"social.near",methodNames:[],accounts:[]})}
  if(!accts||!accts.length)throw new Error("no account returned");
  await linkWallet(accts[0].accountId);m.textContent="Connected as "+accts[0].accountId}
 catch(e){m.textContent="Could not connect Meteor: "+(e&&e.message||e)+". You can type your account id instead."}};

/* ---------- missions ---------- */
function renderMissions(){const box=$("missions");box.replaceChildren();
 MISSIONS.forEach((m,i)=>{const b=el("button",null,"mc"+(SEL&&SEL.id===m.id?" sel":""));b.type="button";
  const top=el("div",null,"top");top.appendChild(el("b",`${i+1}. ${m.name}`));const g=el("span",m.best?m.best.grade:"-","grade"+(m.best?" g-"+m.best.grade:""));g.title=m.best?`Best score ${m.best.score}`:"Not played yet";top.appendChild(g);b.appendChild(top);
  b.appendChild(el("div","★".repeat(m.difficulty)+"☆".repeat(4-m.difficulty),"diff"));b.appendChild(el("div",m.tagline,"tg"));
  b.appendChild(el("div",`Budget ${m.budget} ${m.currency}`,"note"));b.onclick=()=>pick(m.id);box.appendChild(b)})}
function pick(id){SEL=MISSIONS.find(m=>m.id===id);DRAFT={};SEL.parts.forEach(p=>DRAFT[p.id]={mode:p.mode,stars:p.stars});renderMissions();renderDraft()}

function caps(){if(!SEL)return{};const pool=+SEL.budget*0.9;const buys=SEL.parts.filter(p=>DRAFT[p.id].mode==="buy");const tw=buys.reduce((a,p)=>a+DRAFT[p.id].stars,0);
 const o={};buys.forEach(p=>o[p.id]=pool*DRAFT[p.id].stars/tw);return o}
function renderDraft(){if(!SEL)return;$("brief").textContent=SEL.brief+` Budget: ${SEL.budget} ${SEL.currency}, 10% kept in reserve.`;
 const box=$("draft");box.replaceChildren();const cp=caps();
 SEL.parts.forEach(p=>{const d=DRAFT[p.id],r=el("div",null,"prow");
  const a=el("div");a.appendChild(el("div",p.title,"t"));a.appendChild(el("div","Intel: "+p.intel||"","intel"+(/WARNING|Reviews say|Mixed/.test(p.intel)?" warn":"")));if(!p.intel)a.lastChild.textContent="Written by you. No sellers.";r.appendChild(a);
  const b=el("div");
  if(p.flex){const sg=el("div",null,"seg");["make","buy"].forEach(m=>{const x=el("button",m==="make"?"Make it":"Buy it",d.mode===m?"on":"");x.type="button";x.onclick=()=>{d.mode=m;renderDraft()};sg.appendChild(x)});b.appendChild(sg)}
  else b.appendChild(el("span",p.mode==="make"?"You write this":"Must be bought","fixed"));r.appendChild(b);
  const c=el("div");
  if(d.mode==="buy"){const st=el("div",null,"stars");st.setAttribute("role","group");st.setAttribute("aria-label","Priority for "+p.title);for(let k=1;k<=5;k++){const x=el("button","★",k<=d.stars?"on":"");x.type="button";x.setAttribute("aria-label",k+" stars");x.onclick=()=>{d.stars=k;renderDraft()};st.appendChild(x)}c.appendChild(st);c.appendChild(el("div",`Spend cap about ${fmt(cp[p.id])} ${SEL.currency}`,"share"))}
  else c.appendChild(el("div","No spend","share"));r.appendChild(c);box.appendChild(r)});if(!RUNNING)scene.preview(SEL,DRAFT,cp)}

/* ---------- run ---------- */
async function startMission(){$("runErr").textContent="";if(!SEL){$("runErr").textContent="Pick a mission first.";return}
 try{await api("/api/run",{mode:"demo",mission:SEL.id,draft:DRAFT,name:$("pname").value,pace:$("pace").checked?1.1:0});begin("mission");$("stage").scrollIntoView({behavior:"smooth",block:"center"})}catch(e){$("runErr").textContent=e.message}}
async function begin(kind){runKind=kind;seen=0;shownReport=false;finaleQueued=false;needInit=true;RUNNING=true;$("report").replaceChildren();$("arena").replaceChildren();await scene.reset();poll()}

function renderArena(s){const box=$("arena");if(!s.receipts.length){if(s.running&&!box.children.length)box.appendChild(el("div","Opening the job board...","note"));return}
 if(seen===0)box.replaceChildren();const pers=(s.mission&&s.mission.personas)||{};
 s.receipts.forEach((r,i)=>{if(i<seen)return;const c=el("div",null,"enc"),h=el("div",null,"eh");
  const good=r.status==="paid"||r.status==="self_done";
  const av=el("div",r.kind==="make"?"You":(r.seller&&r.seller!=="-"?r.seller[0]:"?"),"av "+(good?"ok":"bad"));h.appendChild(av);
  const nm=el("div");nm.appendChild(el("div",r.title,"nm"));nm.appendChild(el("div",r.kind==="make"?"Done by your own agent":(r.seller!=="-"?`${r.seller}${pers[r.seller]?": "+pers[r.seller]:""}`:"No deal"),"ps"));h.appendChild(nm);
  h.appendChild(el("span",r.status.replace("_"," "),"chip "+r.status));c.appendChild(h);
  const kv=el("div",null,"kv");const add=(k,v)=>{const q=el("div");q.appendChild(el("span",k+" "));q.appendChild(el("em",v));kv.appendChild(q)};
  if(r.kind==="buy"){add("Lowest quote",r.quoted||"-");add("Final price",`${r.final_price} + fee ${r.fee}`);add("Paid out",`${r.total_paid} ${r.currency}`)}c.appendChild(kv);
  const ck=r.check||{};if(ck.reasons)c.appendChild(el("div",(ck.passed?"Check passed: ":"Check FAILED: ")+(ck.reasons||[]).join("; "),"note"));
  const tl=el("ul",null,"tl");
  (r.negotiation||[]).forEach(n=>{const txt=n.action==="counter"?`Quote ${fmt(n.quoted)} was above the cap. Countered once at ${fmt(n.offered)}.`:n.action==="accept"?`Accepted ${n.bidder} at ${fmt(n.amount)} (${n.reason}).`:`Walked away: ${n.reason}.`;tl.appendChild(el("li",txt,n.action==="walk_away"?"bad":(n.action==="accept"?"good":"")))});
  (r.payments||[]).forEach(p=>tl.appendChild(el("li",`Funded escrow: ${fmt(p.amount)} to ${p.to}`,"pay")));
  (r.notes||[]).forEach(n=>tl.appendChild(el("li",n,/withheld|fail/i.test(n)?"bad":"")));if(tl.children.length)c.appendChild(tl);
  const pp=s.part_points[i];if(pp&&runKind==="mission"){const row=el("div",null,"pts");pp.lines.forEach(l=>row.appendChild(el("span",`${l.points>=0?"+":""}${l.points} ${l.label}`,l.points<0?"neg":"")));row.appendChild(el("span",`${pp.total} pts`,"tot"));c.appendChild(row)}
  box.appendChild(c);
  scene.enqueue(()=>scene.playReceipt(r,pp))});
 seen=s.receipts.length}

/* ---------- side panels ---------- */
function renderBudget(s){const box=$("budgetBox");box.replaceChildren();const t=s.totals;if(!t){box.textContent="Starts when the mission starts.";return}
 const b=+t.budget,bar=el("div",null,"hp");[["#2fe6a1",t.spent],["#ffb84d",t.held],["#5b6b82",t.remaining]].forEach(([c,v])=>{const i=el("i");i.style.width=(100*v/b)+"%";i.style.background=c;bar.appendChild(i)});box.appendChild(bar);
 const lg=el("div",null,"leg");[["#2fe6a1","Spent",t.spent],["#ffb84d","Held",t.held],["#5b6b82","Unspent",t.remaining]].forEach(([c,n,v])=>{const d=el("span");const i=el("i");i.style.background=c;d.appendChild(i);d.appendChild(document.createTextNode(`${n} ${fmt(v)}`));lg.appendChild(d)});box.appendChild(lg);
 box.appendChild(el("div",`Budget ${fmt(b)} ${t.currency}. Spend can never exceed it.`,"note")).style.marginTop="8px"}
function renderWallet(s){const w=s.wallet,box=$("wallet");box.replaceChildren();
 $("wbadge").textContent=w?(w.kind==="simulated"?"SIMULATED":"LIVE"):(s.live_missing.length?"NOT CONNECTED":"READY");
 if(!w){box.appendChild(document.createTextNode("When the mission starts, the wallet is funded with exactly the budget. The agent can spend it but cannot change the rules below."));return}
 box.appendChild(el("div",w.balance!==null?`${fmt(w.balance)} ${w.currency}`:"See your Agent API dashboard","big"));
 const kv=el("div",null,"kvl");[["Max per payment",`${fmt(w.max_single_payment)} ${w.currency}`],["May only pay",w.allowed_recipients.join(", ")]].forEach(([k,v])=>{kv.appendChild(el("span",k));kv.appendChild(el("b",v))});box.appendChild(kv);
 if(!w.payments.length)box.appendChild(el("div","No payments yet","pay"));
 w.payments.forEach(p=>box.appendChild(el("div",`${fmt(p.amount)} ${w.currency} to ${p.to} [${p.part_id}]`,"pay")))}

/* ---------- report ---------- */
function renderReport(s){if(!s.game||shownReport)return;shownReport=true;const g=s.game.result,a=s.game.applied,box=$("report");box.replaceChildren();
 const rp=el("div",null,"report"),hd=el("div",null,"rhead");hd.appendChild(el("div",g.grade,"bigg g-"+g.grade));
 const t=el("div");t.appendChild(el("div","MISSION REPORT: "+s.mission.name,"note"));t.appendChild(el("div",`${g.total} pts`,"rs"));
 t.appendChild(el("div",`+${a.xp_gain} XP${a.personal_best?"  New personal best!":""}`,"note"));
 if(a.ranked_up)t.appendChild(el("div",`RANK UP: ${a.rank_before} to ${a.rank_after}`,"rank-up"));hd.appendChild(t);rp.appendChild(hd);
 const cols=el("div",null,"cols"),c1=el("div");c1.appendChild(el("div","Score breakdown","sub"));const tb=el("table");
 const row=(l,p)=>{const tr=el("tr");tr.appendChild(el("td",l));tr.appendChild(el("td",(p>=0?"+":"")+p,p<0?"n":""));tb.appendChild(tr)};
 g.parts.forEach(p=>p.lines.forEach(l=>row(`${p.title}: ${l.label}`,l.points)));g.bonus.forEach(b=>row(b.label,b.points));row("Total",g.total);c1.appendChild(tb);cols.appendChild(c1);
 const c2=el("div");c2.appendChild(el("div","Leaderboard (this computer)","sub"));const lb=el("table",null,"lb");
 a.leaderboard.forEach((r,i)=>{const tr=el("tr",null,r.ghost?"gh":(r.name===PROFILE.name&&r.score===g.total?"me":""));tr.appendChild(el("td",`${i+1}. ${r.name}`));tr.appendChild(el("td",String(r.score)));lb.appendChild(tr)});c2.appendChild(lb);
 if(a.new_badges.length){c2.appendChild(el("div","Badges unlocked","sub")).style.marginTop="14px";const bb=el("div",null,"badges");a.new_badges.forEach(b=>{const d=el("div",null,"bdg");d.appendChild(document.createTextNode("★ "+b.name));d.appendChild(el("small",b.desc));bb.appendChild(d);toast("Badge: "+b.name,"badge")});c2.appendChild(bb)}
 cols.appendChild(c2);rp.appendChild(cols);
 const sh=el("div",null,"sharebox");sh.appendChild(el("div","Share your run (text for the demo post)","sub"));sh.appendChild(el("div",g.share));
 const cb=el("button","Copy text","sec");cb.style.marginTop="8px";cb.onclick=()=>{try{navigator.clipboard.writeText(g.share);cb.textContent="Copied"}catch(e){cb.textContent="Select and copy"}};sh.appendChild(cb);rp.appendChild(sh);
 const nb=el("div",null,"go"),again=el("button","Try again","sec");again.onclick=()=>{$("draft").scrollIntoView({behavior:"smooth"})};nb.appendChild(again);
 const i=MISSIONS.findIndex(m=>m.id===SEL.id);if(MISSIONS[i+1]){const nx=el("button","Next mission");nx.onclick=()=>{pick(MISSIONS[i+1].id);$("draft").scrollIntoView({behavior:"smooth"})};nb.appendChild(nx)}rp.appendChild(nb);
 box.appendChild(rp);box.scrollIntoView({behavior:"smooth",block:"start"});
 renderProfile(a.profile);loadMissions(true)}

/* ---------- poll ---------- */
function render(s){
 $("btnGo").disabled=$("btnDemo").disabled=$("btnLive").disabled=s.running;
 const m=$("mode");m.textContent=s.mode==="live"?"LIVE FUNDS":(s.mode==="demo"?"SIMULATION":"READY");m.className="modechip"+(s.mode==="live"?" live":"");
 const miss=s.live_missing;$("liveNote").textContent=miss.length?("Still needed: "+miss.join(", ")):"All five connection values are set. Run the read-only check first.";if(miss.length)$("btnLive").disabled=true;
 if(s.error){(runKind==="mission"?$("runErr"):$("advErr")).textContent=s.error}
 RUNNING=s.running;if(needInit&&s.plan){scene.start(s.plan);needInit=false}
 renderWallet(s);renderBudget(s);renderArena(s);if(s.result)$("result").textContent=s.result;
 if(s.game&&!finaleQueued){finaleQueued=true;scene.enqueue(()=>scene.finale(s.game.result.grade)).then(()=>renderReport(s))}}
async function poll(){clearTimeout(timer);const s=await api("/api/state");render(s);if(s.running||(s.done&&s.game&&!shownReport))timer=setTimeout(poll,600)}

/* ---------- advanced / live (unchanged behaviour) ---------- */
async function loadMissions(keepSel){const r=await api("/api/missions");MISSIONS=r.missions;renderProfile(r.profile);if(keepSel&&SEL){SEL=MISSIONS.find(m=>m.id===SEL.id)||SEL}renderMissions()}
const adv=async mode=>{$("advErr").textContent="";try{await api("/api/run",{mode,task:$("task").value,budget:$("budget").value,approve:$("approve").value.trim(),pace:mode==="demo"&&$("pace").checked?1.1:0});begin("practice")}catch(e){$("advErr").textContent=e.message}};
$("btnGo").onclick=startMission;$("btnDemo").onclick=()=>adv("demo");$("btnLive").onclick=()=>adv("live");
$("btnPlan").onclick=async()=>{try{const p=await api("/api/plan",{task:$("task").value,budget:$("budget").value,mode:"live"});$("hashOut").textContent=p.hash}catch(e){$("advErr").textContent=e.message}};
$("btnSave").onclick=async()=>{try{await api("/api/connect",{NEAR_MARKET_API_KEY:$("cMk").value,MARKET_DEPOSIT_ADDRESS:$("cAddr").value,NEAR_INTENTS_AGENT_API_KEY:$("cAk").value,AGENT_ID:$("cAid").value,AGENT_GRANT_TOKEN:$("cGt").value});["cMk","cAk","cGt"].forEach(i=>$(i).value="");poll()}catch(e){$("advErr").textContent=e.message}};
$("btnVerify").onclick=async()=>{const box=$("verify");box.replaceChildren(el("div","Checking...","note"));try{const r=await api("/api/verify",{});box.replaceChildren();if(r.error)box.appendChild(el("div",r.error,"err"));r.results.forEach(x=>{const d=el("div",null,"pr");d.appendChild(el("span",x.ok?"OK ":"FAIL ",x.ok?"ok":"no"));d.appendChild(document.createTextNode(x.name+": "+x.detail));box.appendChild(d)})}catch(e){box.replaceChildren(el("div",e.message,"err"))}};
$("btnReset").onclick=async()=>{if(!confirm("Reset XP, badges and your leaderboard entries on this computer?"))return;const r=await api("/api/reset",{});renderProfile(r.profile);loadMissions(true)};
fetch("/api/example",{headers:H}).then(r=>r.text()).then(t=>{$("task").value=JSON.stringify(JSON.parse(t),null,2)});
loadMissions().then(()=>pick(MISSIONS[0].id));poll();
</script></body></html>
"""
