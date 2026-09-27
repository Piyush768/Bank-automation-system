"""Minimal operator console (deliberately bare; see REPORT.md section 5).

It is a real control surface over the *same live session* the automation was
using: live (masked) screenshot, claim, structured actions, hand back with a
resolution mode. When the run is started with --headed the operator can also
just use the actual browser window; those clicks/inputs are captured too.
"""

from __future__ import annotations

import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from cua.escalation.control import ControlChannel
from cua.surface.browser import ControlViolation

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Operator Console</title>
<style>body{font:14px system-ui;margin:16px;max-width:1100px}.row{display:flex;gap:16px;flex-wrap:wrap}
.card{border:1px solid #ccc;border-radius:8px;padding:12px;flex:1;min-width:320px}
img{max-width:100%;border:1px solid #999}pre{white-space:pre-wrap;background:#f4f4f4;padding:8px}
input,select,button{font:inherit;margin:2px}</style></head><body>
<h2>Operator Console</h2><div id=owner></div>
<div class=row><div class=card><h3>Intervention</h3><pre id=iv>none</pre>
<input id=op placeholder="operator id" value="operator-1"><button onclick="claim()">Take control</button>
<h3>Act on the live session</h3>
<input id=ct placeholder="click control with text"><button onclick="cmd('click_text',{text:v('ct')})">Click</button><br>
<input id=fl placeholder="field label"><input id=fv type=password placeholder="value"><button onclick="cmd('fill_label',{label:v('fl'),value:v('fv')})">Fill</button><br>
<input id=nv placeholder="/path"><button onclick="cmd('navigate',{path:v('nv')})">Navigate</button>
<h3>Hand back</h3><select id=mode><option value=resume_verify>resume: verify state and continue</option>
<option value=step_done>I completed the current step</option><option value=retry_step>retry the current step</option>
<option value=approve>approve risky action</option><option value=deny>deny risky action</option>
<option value=abort>abort run</option></select><input id=note placeholder="note"><button onclick="handback()">Hand back</button>
<pre id=out></pre></div>
<div class=card><h3>Live session (PII masked)</h3><img id=shot></div></div>
<script>
const v=id=>document.getElementById(id).value;
async function j(u,b){const r=await fetch(u,{method:b?'POST':'GET',headers:{'content-type':'application/json'},body:b?JSON.stringify(b):undefined});const t=await r.text();document.getElementById('out').textContent=t;return t}
const claim=()=>j('/api/claim',{operator:v('op')}), cmd=(k,a)=>j('/api/command',{kind:k,args:a}),
handback=()=>j('/api/handback',{mode:v('mode'),note:v('note')});
async function tick(){try{const s=await (await fetch('/api/state')).json();document.getElementById('owner').textContent='Session owner: '+s.owner;
document.getElementById('iv').textContent=s.intervention?JSON.stringify(s.intervention,null,1):'none';
if(s.intervention)document.getElementById('shot').src='/api/screenshot?t='+Date.now();}catch(e){}}
setInterval(tick,1500);tick();</script></body></html>"""


class ClaimBody(BaseModel):
    operator: str


class CommandBody(BaseModel):
    kind: str
    args: dict = {}


class HandbackBody(BaseModel):
    mode: str = "resume_verify"
    note: str = ""


def build_console(channel: ControlChannel) -> FastAPI:
    app = FastAPI(title="Operator Console", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/state")
    def state():
        return channel.state()

    @app.post("/api/claim")
    def claim(b: ClaimBody):
        try:
            return channel.claim(b.operator).public()
        except ControlViolation as e:
            raise HTTPException(409, str(e))

    @app.post("/api/command")
    def command(b: CommandBody):
        try:
            return channel.submit(b.kind, b.args)
        except ControlViolation as e:
            raise HTTPException(409, str(e))

    @app.get("/api/screenshot")
    def screenshot():
        if channel.intervention is None:
            raise HTTPException(404, "no active intervention")
        png = channel.submit("screenshot", {}, timeout=15)
        if not isinstance(png, (bytes, bytearray)):
            raise HTTPException(500, str(png))
        return Response(png, media_type="image/png")

    @app.post("/api/handback")
    def handback(b: HandbackBody):
        try:
            return channel.submit("handback", {"mode": b.mode, "note": b.note})
        except ControlViolation as e:
            raise HTTPException(409, str(e))

    return app


class ConsoleServer:
    def __init__(self, channel: ControlChannel, port: int = 8765):
        self.url = f"http://127.0.0.1:{port}"
        cfg = uvicorn.Config(build_console(channel), host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "ConsoleServer":
        self._thread.start()
        for _ in range(100):
            try:
                httpx.get(self.url + "/api/state", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("operator console failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


class ScriptedOperator(threading.Thread):
    """A stand-in human for unattended demos/tests.

    It uses ONLY the console's public HTTP API (the same one a person's browser
    uses), so the demo exercises the real control-transfer path.
    """

    def __init__(self, console_url: str, operator: str, actions: list[tuple[str, dict]],
                 handback: tuple[str, str], wait_s: float = 120):
        super().__init__(daemon=True)
        self.url, self.operator, self.actions, self.handback_args, self.wait_s = console_url, operator, actions, handback, wait_s
        self.log: list[dict] = []

    def run(self) -> None:
        c = httpx.Client(base_url=self.url, timeout=30)
        deadline = time.time() + self.wait_s
        while time.time() < deadline:
            s = c.get("/api/state").json()
            if s["intervention"] and s["owner"] == "awaiting_operator":
                break
            time.sleep(0.2)
        else:
            return
        self.log.append({"claim": c.post("/api/claim", json={"operator": self.operator}).json()["id"]})
        c.get("/api/screenshot")  # an operator looks before acting
        self.log.append({"observe": c.post("/api/command", json={"kind": "observe", "args": {}}).json()})
        for kind, args in self.actions:
            self.log.append({kind: c.post("/api/command", json={"kind": kind, "args": args}).json()})
        mode, note = self.handback_args
        self.log.append({"handback": c.post("/api/handback", json={"mode": mode, "note": note}).json()})
