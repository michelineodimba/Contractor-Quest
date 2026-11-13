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

from . import runner
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
    return {"running": st.running, "done": st.done, "mode": st.mode, "error": st.error, "plan": st.plan,
            "totals": totals, "wallet": wallet, "receipts": [r.to_dict() for r in (c.store.receipts if c else [])],
            "result": st.result, "live_missing": runner.live_env_missing(),
            "connected": {k: bool(os.environ.get(k)) for k in CONNECT_KEYS}}


def start_run(st: State, mode, task_text, budget, approve, out, pace=0):
    with st.lock:
        if st.running:
            raise RuntimeError("a run is already in progress")
        st.running, st.done, st.error, st.result, st.contractor, st.plan, st.mode = True, False, None, None, None, None, mode

    def work():
        try:
            task = runner.parse_task(task_text, budget)
            if mode == "live":
                plan, c = runner.make_live(task, out, approve)
            else:
                plan, c = runner.make_demo(task, out, pace=pace)
            st.plan, st.contractor = plan_dict(plan), c
            rep = c.run(task, plan)
            st.result = Path(rep.result_path).read_text()
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
                    start_run(st, body.get("mode", "demo"), body["task"], body.get("budget"),
                              body.get("approve"), out, pace)
                    self._send(202, json.dumps({"started": True}))
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st, token, allowed, out))
    actual = srv.server_address[1]
    allowed.update({f"127.0.0.1:{actual}", f"localhost:{actual}"})
    return srv, st, token


def serve(port=8765, out="runs", open_browser=True):
    srv, _, token = make_server(port, out)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"NEAR Contractor UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEAR Contractor</title>
<style>
:root{--bg:#f3f5f9;--card:#fff;--tx:#12161d;--mut:#5b6575;--bd:#e1e5ec;--ac:#00a86b;--ac2:#0b6bcb;--ok:#0f7b3f;--bad:#c0362c;--warn:#b27a00;--soft:#f0f3f8;--shadow:0 1px 2px rgba(16,24,40,.06),0 4px 14px rgba(16,24,40,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0c0e12;--card:#151921;--tx:#e9ecf1;--mut:#98a2b3;--bd:#262c38;--ac:#2fd69a;--ac2:#5aa2ff;--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341;--soft:#1c212b;--shadow:none}}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 -apple-system,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--tx)}
header{background:linear-gradient(120deg,#062b21,#0a4a63 60%,#10315f);color:#fff;padding:26px 20px 70px}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px}.hrow{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{width:42px;height:42px;border-radius:11px;background:#fff;color:#062b21;display:grid;place-items:center;font-weight:800;font-size:22px}
h1{margin:0;font-size:24px;letter-spacing:-.3px}.tag{opacity:.85;font-size:14px;max-width:640px}
.mode{margin-left:auto;padding:5px 12px;border-radius:99px;font-size:12px;font-weight:700;letter-spacing:.4px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3)}
.mode.sim{background:#e3b34133;border-color:#e3b341}.mode.live{background:#ff7b7233;border-color:#ff7b72}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;margin-top:-48px;padding-bottom:40px}
@media (max-width:900px){.grid{grid-template-columns:1fr}}aside{display:flex;flex-direction:column;gap:18px}
@media (min-width:901px){aside{position:sticky;top:14px;align-self:start}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:18px;box-shadow:var(--shadow);margin-bottom:18px}aside .card{margin-bottom:0}
.ct{display:flex;align-items:center;gap:10px;margin:0 0 12px;font-size:16px;font-weight:700}.num{width:24px;height:24px;border-radius:50%;background:var(--ac);color:#04251a;display:grid;place-items:center;font-size:13px;font-weight:800}
.sp{margin-left:auto;font-size:11px;font-weight:700;padding:2px 9px;border-radius:99px;background:var(--soft);color:var(--mut);border:1px solid var(--bd)}
textarea,input{font:inherit;color:var(--tx);background:var(--soft);border:1px solid var(--bd);border-radius:8px;padding:8px 10px}textarea{width:100%;height:200px;font:12px/1.45 ui-monospace,Consolas,monospace}
input[type=password],input[type=text]{width:100%}button{font:inherit;font-weight:600;padding:9px 16px;border-radius:9px;border:1px solid var(--ac);background:var(--ac);color:#04251a;cursor:pointer}
button.sec{background:transparent;color:var(--tx);border-color:var(--bd)}button.danger{background:var(--bad);border-color:var(--bad);color:#fff}button:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}.note{color:var(--mut);font-size:13px}.err{color:var(--bad);margin-top:8px;white-space:pre-wrap;font-size:14px}
.tsum{padding:12px 14px;background:var(--soft);border-radius:10px;border:1px solid var(--bd)}.tsum b{font-size:16px}
.prow{display:grid;grid-template-columns:minmax(0,1.3fr) 92px minmax(60px,1fr) 100px;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--bd);font-size:14px}.prow:last-child{border:0}
.pbar{height:8px;background:var(--soft);border-radius:99px;overflow:hidden}.pbar i{display:block;height:100%;background:var(--ac);border-radius:99px}.pcap{text-align:right;font-variant-numeric:tabular-nums}
.tag2{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;text-align:center;border:1px solid var(--bd);background:var(--soft)}.tag2.buy{color:var(--ac2)}.tag2.make{color:var(--mut)}
code{background:var(--soft);padding:1px 6px;border-radius:5px;font-size:12px}
.chip{padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid currentColor}.paid,.self_done{color:var(--ok)}.withheld,.error,.failed_check,.no_deal,.unresolved{color:var(--bad)}
.rc{border:1px solid var(--bd);border-radius:12px;padding:14px;margin-top:10px;background:var(--card)}.rh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.rh b{font-size:15px}
.kv{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0;font-size:14px}.kv span{color:var(--mut)}.kv em{font-style:normal;font-weight:700;color:var(--tx)}
.tl{list-style:none;margin:8px 0 0;padding:0 0 0 14px;border-left:2px solid var(--bd)}.tl li{position:relative;margin:6px 0;padding-left:12px;font-size:13px}
.tl li:before{content:"";position:absolute;left:-19px;top:6px;width:8px;height:8px;border-radius:50%;background:var(--mut)}.tl li.good:before{background:var(--ok)}.tl li.bad:before{background:var(--bad)}.tl li.pay:before{background:var(--ac2)}
.big{font-size:30px;font-weight:800;letter-spacing:-.5px;font-variant-numeric:tabular-nums}.kvl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px;margin-top:10px}.kvl span{color:var(--mut)}.kvl b{word-break:break-all;text-align:right}
.pay{font-size:12px;padding:7px 9px;background:var(--soft);border-radius:8px;margin-top:6px;word-break:break-all}
.donutw{display:flex;gap:16px;align-items:center}.leg{font-size:13px}.leg div{display:flex;align-items:center;gap:7px;margin:3px 0}.leg i{width:10px;height:10px;border-radius:3px;display:inline-block}
pre{white-space:pre-wrap;background:var(--soft);padding:12px;border-radius:10px;max-height:300px;overflow:auto;font-size:13px;margin:0}
details>summary{cursor:pointer;font-weight:600;padding:6px 0}.field{margin-top:8px}.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:3px}
.pr{font-size:13px;padding:6px 0;border-bottom:1px solid var(--bd);word-break:break-word}.pr:last-child{border:0}.ok{color:var(--ok);font-weight:700}.no{color:var(--bad);font-weight:700}
</style></head><body>
<header><div class="wrap hrow"><div class="logo">N</div><div><h1>NEAR Contractor</h1><div class="tag">An agent with a wallet and a budget: it splits a job, hires other agents, negotiates once, checks the work, and only pays for what passes.</div></div><div class="mode" id="mode">READY</div></div></header>

<div class="wrap grid"><div>
<div class="card"><div class="ct"><span class="num">1</span>Your task<span class="sp">what to get done</span></div>
<div class="tsum"><b id="taskTitle"></b><div class="note" id="taskDesc"></div><div class="note" id="taskMeta"></div></div>
<div class="row"><label>Budget <input id="budget" size="7" value="20"></label><span class="note" id="cur">USDC</span><button class="sec" id="btnPlan">Preview plan</button></div>
<details style="margin-top:8px"><summary>Edit the task (advanced)</summary><textarea id="task" spellcheck="false"></textarea></details><div class="err" id="planErr"></div></div>

<div class="card"><div class="ct"><span class="num">2</span>Plan<span class="sp">budget split</span></div><div id="plan" class="note">Click "Preview plan" to see how the budget is split between what the agent does itself and what it buys.</div></div>

<div class="card"><div class="ct"><span class="num">3</span>Run</div>
<div class="row" style="margin-top:0"><button id="btnDemo">Run simulated demo</button><label class="note"><input type="checkbox" id="pace" checked> show it step by step</label></div>
<div class="note" style="margin-top:6px">Safe: fake market and wallet. No network, no funds.</div>
<details id="liveBox" style="margin-top:12px"><summary>Live mode: connect to the real market and wallet</summary>
<div class="note">Keys are kept in this program's memory only. They are never saved to disk or shown again.</div>
<div class="field"><label>market.near.ai API key</label><input type="password" id="cMk" autocomplete="off"></div>
<div class="field"><label>Market deposit address (the one address the wallet may pay)</label><input type="text" id="cAddr" autocomplete="off"></div>
<div class="field"><label>Agent API key</label><input type="password" id="cAk" autocomplete="off"></div>
<div class="field"><label>Agent ID</label><input type="text" id="cAid" autocomplete="off"></div>
<div class="field"><label>Agent grant token</label><input type="password" id="cGt" autocomplete="off"></div>
<div class="row"><button class="sec" id="btnSave">Save for this session</button><button class="sec" id="btnVerify">Check connection (read-only)</button></div>
<div id="verify"></div>
<div class="note" id="liveNote" style="margin-top:8px"></div>
<div class="row"><input id="approve" size="22" placeholder="type the plan hash to approve"><button class="danger" id="btnLive">Run live (spends real funds)</button></div>
<div class="note">It will not start unless the hash you type matches the previewed plan and budget.</div></details>
<div class="err" id="runErr"></div></div>

<div class="card"><div class="ct"><span class="num">4</span>Receipts<span class="sp" id="rstat">nothing yet</span></div><div id="recs" class="note">One receipt per part: what was bought, from whom, at what price, and whether the work passed the check.</div></div>
<div class="card"><div class="ct">Final result</div><pre id="result">(nothing yet)</pre></div>
</div>

<aside>
<div class="card"><div class="ct">Agent wallet<span class="sp" id="wbadge">NOT STARTED</span></div><div id="wallet"></div></div>
<div class="card"><div class="ct">Budget</div><div id="budgetBox" class="note">Starts when a run begins.</div></div>
</aside></div>

<script>
const TOKEN="__TOKEN__",$=id=>document.getElementById(id);
const H={"X-UI-Token":TOKEN,"Content-Type":"application/json"};
const api=(p,b)=>fetch(p,b?{method:"POST",headers:H,body:JSON.stringify(b)}:{headers:H}).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j});
const el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e};
const fmt=n=>(+n).toFixed(2);let timer=null;
fetch("/api/example",{headers:H}).then(r=>r.text()).then(t=>{$("task").value=JSON.stringify(JSON.parse(t),null,2);taskSummary()});
function taskSummary(){let t=null;try{t=JSON.parse($("task").value)}catch(e){}
 $("taskTitle").textContent=t?t.title:"The task JSON is not valid";$("taskDesc").textContent=t?(t.description||""):"";
 $("taskMeta").textContent=t?`${(t.parts||[]).length} parts, ${(t.parts||[]).filter(p=>p.mode==="buy").length} to buy`:"";if(t&&t.currency)$("cur").textContent=t.currency}
$("task").oninput=taskSummary;

function renderPlan(p){const box=$("plan");box.replaceChildren();
 const h=el("div",null,"note");h.appendChild(document.createTextNode(`Budget ${fmt(p.budget)} ${p.currency} = caps for bought parts + ${fmt(p.reserve)} reserve. Plan hash `));h.appendChild(el("code",p.hash));box.appendChild(h);
 const max=Math.max(1,...p.parts.map(x=>+x.cap));
 p.parts.forEach(x=>{const r=el("div",null,"prow");r.appendChild(el("div",x.title));r.appendChild(el("span",x.mode==="make"?"does itself":"buys","tag2 "+x.mode));
  const b=el("div",null,"pbar"),i=el("i");i.style.width=(x.mode==="make"?0:100*x.cap/max)+"%";b.appendChild(i);r.appendChild(b);
  r.appendChild(el("div",x.mode==="make"?"-":`${fmt(x.cap)} ${p.currency}`,"pcap"));box.appendChild(r)})}
async function preview(mode){$("planErr").textContent="";
 try{const p=await api("/api/plan",{task:$("task").value,budget:$("budget").value,mode});renderPlan(p);return p}catch(e){$("planErr").textContent=e.message}}
$("btnPlan").onclick=()=>preview("demo");

async function run(mode){$("runErr").textContent="";
 try{await api("/api/run",{mode,task:$("task").value,budget:$("budget").value,approve:$("approve").value.trim(),pace:$("pace").checked&&mode==="demo"?1.1:0});poll()}catch(e){$("runErr").textContent=e.message}}
$("btnDemo").onclick=async()=>{await preview("demo");run("demo")};
$("btnLive").onclick=async()=>{const p=await preview("live");if(p)run("live")};
$("btnSave").onclick=async()=>{try{await api("/api/connect",{NEAR_MARKET_API_KEY:$("cMk").value,MARKET_DEPOSIT_ADDRESS:$("cAddr").value,NEAR_INTENTS_AGENT_API_KEY:$("cAk").value,AGENT_ID:$("cAid").value,AGENT_GRANT_TOKEN:$("cGt").value});
 ["cMk","cAk","cGt"].forEach(i=>$(i).value="");poll()}catch(e){$("runErr").textContent=e.message}};
$("btnVerify").onclick=async()=>{const box=$("verify");box.replaceChildren(el("div","Checking...","note"));
 try{const r=await api("/api/verify",{});box.replaceChildren();if(r.error)box.appendChild(el("div",r.error,"err"));
  r.results.forEach(x=>{const d=el("div",null,"pr");d.appendChild(el("span",x.ok?"OK ":"FAIL ",x.ok?"ok":"no"));d.appendChild(document.createTextNode(x.name+": "+x.detail));box.appendChild(d)})}catch(e){box.replaceChildren(el("div",e.message,"err"))}};

function donut(t){const NS="http://www.w3.org/2000/svg",C=2*Math.PI*40,svg=document.createElementNS(NS,"svg");svg.setAttribute("viewBox","0 0 100 100");svg.setAttribute("width","110");svg.setAttribute("height","110");
 const seg=[[+t.spent,"#2fd69a"],[+t.held,"#e3b341"],[+t.remaining,"#8a93a3"]],b=+t.budget;let off=0;
 const bg=document.createElementNS(NS,"circle");["cx","cy"].forEach(k=>bg.setAttribute(k,"50"));bg.setAttribute("r","40");bg.setAttribute("fill","none");bg.setAttribute("stroke","#8a93a322");bg.setAttribute("stroke-width","14");svg.appendChild(bg);
 seg.forEach(([v,c])=>{const len=C*v/b,k=document.createElementNS(NS,"circle");["cx","cy"].forEach(q=>k.setAttribute(q,"50"));k.setAttribute("r","40");k.setAttribute("fill","none");k.setAttribute("stroke",c);k.setAttribute("stroke-width","14");
  k.setAttribute("stroke-dasharray",`${len} ${C-len}`);k.setAttribute("stroke-dashoffset",String(-off));k.setAttribute("transform","rotate(-90 50 50)");svg.appendChild(k);off+=len});return svg}
function renderBudget(s){const box=$("budgetBox");box.replaceChildren();const t=s.totals;if(!t){box.appendChild(el("div","Starts when a run begins.","note"));return}
 const w=el("div",null,"donutw");w.appendChild(donut(t));const lg=el("div",null,"leg");
 [["#2fd69a","Spent",t.spent],["#e3b341","Held (escrow / disputes)",t.held],["#8a93a3","Unspent",t.remaining]].forEach(([c,n,v])=>{const d=el("div");const i=el("i");i.style.background=c;d.appendChild(i);d.appendChild(document.createTextNode(`${n}: ${fmt(v)}`));lg.appendChild(d)});
 w.appendChild(lg);box.appendChild(w);box.appendChild(el("div",`Budget ${fmt(t.budget)} ${t.currency}. Spend can never exceed it.`,"note"))}
function renderWallet(s){const w=s.wallet,box=$("wallet");box.replaceChildren();
 $("wbadge").textContent=w?(w.kind==="simulated"?"SIMULATED":"LIVE"):(s.live_missing.length?"NOT CONNECTED":"READY");
 if(!w){box.appendChild(el("div","When a run starts, the wallet is funded with exactly the task budget. The agent can spend from it, but it cannot change the rules below. Only the owner can.","note"));return}
 box.appendChild(el("div",w.balance!==null?`${fmt(w.balance)} ${w.currency}`:"See your Agent API dashboard","big"));box.appendChild(el("div","wallet balance","note"));
 const kv=el("div",null,"kvl");[["Budget",`${fmt(w.budget)} ${w.currency}`],["Currency",w.currency],["Max per payment",`${fmt(w.max_single_payment)} ${w.currency}`],["May only pay",w.allowed_recipients.join(", ")]].forEach(([k,v])=>{kv.appendChild(el("span",k));kv.appendChild(el("b",v))});box.appendChild(kv);
 box.appendChild(el("div","Payments","note")).style.marginTop="12px";
 if(!w.payments.length)box.appendChild(el("div","none yet","pay"));
 w.payments.forEach(p=>box.appendChild(el("div",`${fmt(p.amount)} ${w.currency} to ${p.to}  [${p.part_id}, ${p.status}, ${p.op_id}]`,"pay")))}
function renderReceipts(s){const box=$("recs");box.replaceChildren();$("rstat").textContent=s.running?"running...":(s.done?"finished":"nothing yet");
 if(!s.receipts.length){box.appendChild(el("div","One receipt per part: what was bought, from whom, at what price, and whether the work passed the check.","note"));return}
 s.receipts.forEach(r=>{const c=el("div",null,"rc"),h=el("div",null,"rh");h.appendChild(el("b",r.title));h.appendChild(el("span",r.status,"chip "+r.status));c.appendChild(h);
  const kv=el("div",null,"kv");const add=(k,v)=>{const s2=el("div");s2.appendChild(el("span",k+" "));s2.appendChild(el("em",v));kv.appendChild(s2)};
  if(r.kind==="make"){add("Done by","the agent itself")}else{add("Bought from",r.seller);add("Lowest quote",r.quoted||"-");add("Final price",`${r.final_price} + fee ${r.fee}`);add("Paid out",`${r.total_paid} ${r.currency}`)}
  c.appendChild(kv);
  const ck=r.check||{};c.appendChild(el("div",(ck.passed?"Check passed: ":"Check FAILED: ")+(ck.reasons||[]).join("; "),"note"));
  const tl=el("ul",null,"tl");
  (r.negotiation||[]).forEach(n=>{const txt=n.action==="counter"?`Quote ${fmt(n.quoted)} was above plan. Countered once at ${fmt(n.offered)}.`:n.action==="accept"?`Accepted ${n.bidder} at ${fmt(n.amount)} (${n.reason}).`:`Walked away: ${n.reason}.`;
   tl.appendChild(el("li",txt,n.action==="walk_away"?"bad":(n.action==="accept"?"good":"")))});
  (r.payments||[]).forEach(p=>tl.appendChild(el("li",`Funded escrow: ${fmt(p.amount)} to ${p.to} (${p.op_id})`,"pay")));
  (r.notes||[]).forEach(n=>tl.appendChild(el("li",n,/withheld|fail/i.test(n)?"bad":"")));
  if(tl.children.length)c.appendChild(tl);box.appendChild(c)})}

function render(s){
 $("btnDemo").disabled=$("btnLive").disabled=s.running;
 const m=$("mode");m.textContent=s.mode==="live"?"LIVE":(s.mode==="demo"?"SIMULATION":"READY");m.className="mode "+(s.mode==="live"?"live":(s.mode==="demo"?"sim":""));
 const miss=s.live_missing;$("liveNote").textContent=miss.length?("Still needed: "+miss.join(", ")):"All five connection values are set. Run the read-only check first.";if(miss.length)$("btnLive").disabled=true;
 if(s.plan)renderPlan(s.plan);if(s.error)$("runErr").textContent=s.error;
 renderWallet(s);renderBudget(s);renderReceipts(s);if(s.result)$("result").textContent=s.result}
async function poll(){clearTimeout(timer);const s=await api("/api/state");render(s);if(s.running)timer=setTimeout(poll,600)}
poll();
</script></body></html>"""
