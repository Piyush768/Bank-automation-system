"""CoreLine Teller 4.2: a deliberately *hostile* legacy back-office app.

This is the proxy target for the project. It imitates the properties the brief
calls out for real credit-union back-office software:

* server-rendered, table-based layout, <font> tags, no test IDs, no <label>s
  (the visible "label" is just text in the neighbouring table cell)
* generated field names (``f_0001x``) that carry no meaning
* one important control is a <span onclick> that is *not* a button in the
  accessibility tree (so role-based locators alone cannot find it)
* balances live in a table whose row order differs per member, so a
  positional "row 1, column 3" locator silently reads the wrong number
* real runtime conditions: validation errors, not-found, permission denial,
  session expiry, interstitial notices, slowness, 500 errors, and an unknown
  "supervisor override" screen that automation has never seen before

A second *tenant skin* (``tenant=b``) renames labels and buttons, standing in
for another institution running the same vendor product with its own config.

All data is synthetic. Nothing here is a real institution or real PII.
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# --------------------------------------------------------------------------
# Synthetic data
# --------------------------------------------------------------------------

MEMBERS: dict[str, dict] = {
    "10492": {
        "name": "DALE R. WINTERS",
        "ssn": "521-44-9087",
        "since": "03/14/2009",
        # Savings is the FIRST row for this member ...
        "accounts": [
            ("S01", "PRIMARY SAVINGS", "14,250.80"),
            ("D10", "SHARE DRAFT CHECKING", "2,310.44"),
            ("L01", "AUTO LOAN", "(8,120.00)"),
        ],
    },
    "20318": {
        "name": "MARISOL K. ADEYEMI",
        "ssn": "610-12-3344",
        "since": "11/02/2016",
        # ... but the SECOND row for this one (positional locators break).
        "accounts": [
            ("D10", "SHARE DRAFT CHECKING", "915.02"),
            ("S01", "PRIMARY SAVINGS", "3,402.17"),
            ("C05", "12MO SHARE CERTIFICATE", "10,000.00"),
        ],
    },
    "30777": {
        "name": "HENRY T. OKAFOR",
        "ssn": "433-90-1212",
        "since": "07/21/2021",
        "accounts": [("S01", "PRIMARY SAVINGS", "58.00")],
    },
}
RESTRICTED = {"10007"}  # employee accounts: tellers may not view them

USERS = {"teller01": "demo-pass-123"}

# Tenant skins: same vendor product, different configuration/branding.
SKINS = {
    "a": {
        "institution": "HARBOR POINT CREDIT UNION",
        "bg": "#c0c0c0",
        "menu_inquiry": "Member Inquiry",
        "member_label": "Member Number:",
        "search_btn": "INQUIRE",
        "detail_title": "MEMBER INQUIRY - DETAIL",
        "balance_col": "BALANCE",
    },
    "b": {
        "institution": "LAKESHORE FEDERAL CU",
        "bg": "#d8d0b8",
        "menu_inquiry": "Account Holder Lookup",
        "member_label": "Acct Holder #:",
        "search_btn": "SEARCH",
        "detail_title": "ACCOUNT HOLDER - DETAIL",
        "balance_col": "CUR BAL",
    },
}

# --------------------------------------------------------------------------
# Runtime state + fault injection (test harness only)
# --------------------------------------------------------------------------


@dataclass
class Faults:
    """One-shot and persistent faults, toggled via /_harness/faults.

    This endpoint exists only so tests and the demo can inject the runtime
    conditions the brief lists. A real target obviously has no such thing.
    """

    slow_ms: int = 0              # add latency to every page
    interstitial_once: bool = False  # system notice after next sign-on
    expire_once: bool = False     # expire the session on next inquiry
    errors_remaining: int = 0     # 500 "ABEND" on the next N inquiry results
    supervisor_once: bool = False  # unknown override screen on next detail


@dataclass
class State:
    sessions: dict[str, float] = field(default_factory=dict)  # sid -> last seen
    session_ttl_s: int = 900
    faults: Faults = field(default_factory=Faults)
    posted_transfers: list[dict] = field(default_factory=list)


def create_app(tenant: str = "a") -> FastAPI:
    skin = SKINS[tenant]
    state = State()
    app = FastAPI(title="CoreLine Teller 4.2 (mock)", docs_url=None, redoc_url=None)
    app.state.cua = state

    # ---------------- helpers ----------------

    def page(title: str, body: str) -> HTMLResponse:
        if state.faults.slow_ms:
            time.sleep(state.faults.slow_ms / 1000)
        return HTMLResponse(
            f"""<html><head><title>CoreLine Teller 4.2</title></head>
<body bgcolor="{skin['bg']}" style="font-family: 'Courier New', monospace">
<table width="760" border="0" cellpadding="2" cellspacing="0">
<tr><td bgcolor="#000080"><font color="#ffffff" size="2"><b>CORELINE TELLER 4.2
&nbsp;|&nbsp; {html.escape(skin['institution'])}</b></font></td></tr>
<tr><td><font size="3"><b>{html.escape(title)}</b></font></td></tr>
<tr><td>{body}</td></tr>
<tr><td><font size="1" color="#404040">F1=HELP F3=EXIT &nbsp; TERM T0419</font></td></tr>
</table></body></html>"""
        )

    def session_ok(request: Request) -> bool:
        sid = request.cookies.get("CLSESS")
        seen = state.sessions.get(sid or "")
        if not seen or time.time() - seen > state.session_ttl_s:
            state.sessions.pop(sid or "", None)
            return False
        state.sessions[sid] = time.time()
        return True

    def expired() -> RedirectResponse:
        return RedirectResponse("/signon?msg=expired", status_code=303)

    def err(msg: str) -> str:
        return f'<font color="#cc0000"><b>{html.escape(msg)}</b></font><br><br>'

    # ---------------- harness ----------------

    @app.post("/_harness/faults")
    async def set_faults(request: Request):
        data = await request.json()
        for k, v in data.items():
            if hasattr(state.faults, k):
                setattr(state.faults, k, v)
        if "session_ttl_s" in data:
            state.session_ttl_s = int(data["session_ttl_s"])
        return {"faults": state.faults.__dict__, "session_ttl_s": state.session_ttl_s}

    @app.post("/_harness/reset")
    def reset():
        state.faults = Faults()
        state.session_ttl_s = 900
        state.posted_transfers.clear()
        return {"ok": True}

    @app.get("/_harness/transfers")
    def transfers():
        return state.posted_transfers

    # ---------------- sign on ----------------

    @app.get("/")
    def root():
        return RedirectResponse("/signon", status_code=303)

    @app.get("/signon")
    def signon(msg: str = ""):
        banner = ""
        if msg == "expired":
            banner = err("SESSION EXPIRED - PLEASE SIGN ON AGAIN")
        elif msg == "bad":
            banner = err("E-001 INVALID USER ID OR PASSWORD")
        body = f"""{banner}
<form method="post" action="/signon">
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><font size="2">USER ID</font></td><td><input type="text" name="u_fld01" size="12"></td></tr>
<tr><td><font size="2">PASSWORD</font></td><td><input type="password" name="u_fld02" size="12"></td></tr>
<tr><td></td><td><input type="submit" value="SIGN ON"></td></tr>
</table></form>"""
        return page("SIGN ON", body)

    @app.post("/signon")
    def do_signon(u_fld01: str = Form(""), u_fld02: str = Form("")):
        if USERS.get(u_fld01) != u_fld02:
            return RedirectResponse("/signon?msg=bad", status_code=303)
        sid = secrets.token_hex(12)
        state.sessions[sid] = time.time()
        dest = "/notice" if state.faults.interstitial_once else "/main"
        resp = RedirectResponse(dest, status_code=303)
        resp.set_cookie("CLSESS", sid, httponly=True)
        return resp

    @app.get("/notice")
    def notice(request: Request):
        if not session_ok(request):
            return expired()
        state.faults.interstitial_once = False
        body = """<table border="2" cellpadding="8" bgcolor="#ffffcc"><tr><td>
<b>SYSTEM NOTICE</b><br>Scheduled maintenance tonight 23:00-01:00 ET.<br>
Batch posting will be delayed.<br><br>
<a href="/main">CONTINUE</a></td></tr></table>"""
        return page("SYSTEM NOTICE", body)

    # ---------------- main menu ----------------

    @app.get("/main")
    def main(request: Request):
        if not session_ok(request):
            return expired()
        body = f"""<table border="0" cellpadding="3">
<tr><td>01</td><td><a href="/inq">{html.escape(skin['menu_inquiry'])}</a></td></tr>
<tr><td>02</td><td><a href="/xfer">Funds Transfer</a></td></tr>
<tr><td>03</td><td><a href="/signon">Sign Off</a></td></tr>
</table>"""
        return page("MAIN MENU", body)

    # ---------------- inquiry ----------------

    def inquiry_form(banner: str = "", value: str = "") -> HTMLResponse:
        # NOTE: the search control is a <span onclick>, not a <button>.
        body = f"""{banner}
<form method="get" action="/inq/result" name="f1">
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><font size="2">{html.escape(skin['member_label'])}</font></td>
<td><input type="text" name="f_0001x" size="10" value="{html.escape(value)}"></td></tr>
<tr><td></td><td><span onclick="document.forms['f1'].submit()"
 style="cursor:pointer;border:2px outset #fff;background:#e0e0e0;padding:2px 10px">
{html.escape(skin['search_btn'])}</span></td></tr>
</table></form>"""
        return page("MEMBER INQUIRY", body)

    @app.get("/inq")
    def inq(request: Request):
        if not session_ok(request):
            return expired()
        return inquiry_form()

    @app.get("/inq/result")
    def inq_result(request: Request, f_0001x: str = ""):
        if not session_ok(request):
            return expired()
        if state.faults.expire_once:
            state.faults.expire_once = False
            state.sessions.clear()
            return expired()
        if state.faults.errors_remaining > 0:
            state.faults.errors_remaining -= 1
            return HTMLResponse(
                "<html><body><h3>SYSTEM ERROR</h3><pre>ABEND S0C7 IN MODULE CLINQ040"
                "\nCONTACT HELP DESK</pre></body></html>",
                status_code=500,
            )
        mn = f_0001x.strip()
        if not (mn.isdigit() and len(mn) == 5):
            return inquiry_form(err("E-102 INVALID MEMBER NUMBER FORMAT (5 DIGITS)"), mn)
        if mn in RESTRICTED:
            return inquiry_form(err("E-403 ACCESS DENIED - RESTRICTED ACCOUNT"), mn)
        member = MEMBERS.get(mn)
        if not member:
            return inquiry_form(err(f"E-404 NO MEMBER ON FILE FOR {mn}"), mn)
        body = f"""<font size="2">1 MATCH</font>
<table border="1" cellpadding="3" cellspacing="0">
<tr bgcolor="#a0a0a0"><td>MBR NO</td><td>NAME</td><td>OPENED</td></tr>
<tr><td>{mn}</td><td><a href="/mbr?id={mn}">{html.escape(member['name'])}</a></td>
<td>{member['since']}</td></tr></table>"""
        return page("INQUIRY RESULTS", body)

    @app.get("/mbr")
    def member_detail(request: Request, id: str = ""):
        if not session_ok(request):
            return expired()
        if state.faults.supervisor_once:
            # A screen the automation has never seen: needs a human.
            body = """<table border="2" cellpadding="8" bgcolor="#ffdddd"><tr><td>
<b>SUPERVISOR OVERRIDE REQUIRED</b><br>Account flagged for review (code W-77).<br>
<form method="post" action="/override"><input type="hidden" name="id" value="%s">
<table border="0"><tr><td>OVERRIDE CODE</td><td><input type="password" name="ovr" size="6"></td></tr>
<tr><td></td><td><input type="submit" value="ACKNOWLEDGE"></td></tr></table></form></td></tr></table>""" % html.escape(id)
            return page("HOLD", body)
        member = MEMBERS.get(id)
        if not member:
            return inquiry_form(err(f"E-404 NO MEMBER ON FILE FOR {id}"), id)
        rows = "".join(
            f"<tr><td>{s}</td><td>{html.escape(d)}</td><td align=right>${b}</td></tr>"
            for s, d, b in member["accounts"]
        )
        body = f"""<table border="0" cellpadding="2">
<tr><td>MBR NO</td><td><b>{id}</b></td></tr>
<tr><td>NAME</td><td>{html.escape(member['name'])}</td></tr>
<tr><td>SSN</td><td>{member['ssn']}</td></tr>
<tr><td>MEMBER SINCE</td><td>{member['since']}</td></tr></table><br>
<table border="1" cellpadding="3" cellspacing="0">
<tr bgcolor="#a0a0a0"><td>SFX</td><td>DESCRIPTION</td><td>{html.escape(skin['balance_col'])}</td></tr>
{rows}</table><br><a href="/inq">NEW INQUIRY</a> &nbsp; <a href="/main">MAIN MENU</a>"""
        return page(skin["detail_title"], body)

    @app.post("/override")
    def override(request: Request, id: str = Form(""), ovr: str = Form("")):
        if not session_ok(request):
            return expired()
        if ovr != "4242":
            state.faults.supervisor_once = True
            return page("HOLD", err("E-771 OVERRIDE CODE REJECTED") + '<a href="/main">MAIN MENU</a>')
        state.faults.supervisor_once = False
        return RedirectResponse(f"/mbr?id={id}", status_code=303)

    # ---------------- funds transfer (irreversible) ----------------

    @app.get("/xfer")
    def xfer(request: Request):
        if not session_ok(request):
            return expired()
        body = """<form method="post" action="/xfer/review"><table border="1" cellpadding="4" cellspacing="0">
<tr><td>FROM MBR/SFX</td><td><input type="text" name="x_fr" size="10"></td></tr>
<tr><td>TO MBR/SFX</td><td><input type="text" name="x_to" size="10"></td></tr>
<tr><td>AMOUNT</td><td><input type="text" name="x_amt" size="10"></td></tr>
<tr><td></td><td><input type="submit" value="REVIEW"></td></tr></table></form>"""
        return page("FUNDS TRANSFER", body)

    @app.post("/xfer/review")
    def xfer_review(request: Request, x_fr: str = Form(""), x_to: str = Form(""), x_amt: str = Form("")):
        if not session_ok(request):
            return expired()
        e = html.escape
        body = f"""<table border="1" cellpadding="4" cellspacing="0">
<tr><td>FROM</td><td>{e(x_fr)}</td></tr><tr><td>TO</td><td>{e(x_to)}</td></tr>
<tr><td>AMOUNT</td><td>${e(x_amt)}</td></tr></table><br>
<form method="post" action="/xfer/post">
<input type="hidden" name="x_fr" value="{e(x_fr)}"><input type="hidden" name="x_to" value="{e(x_to)}">
<input type="hidden" name="x_amt" value="{e(x_amt)}">
<input type="submit" value="POST TRANSFER"></form>"""
        return page("CONFIRM TRANSFER", body)

    @app.post("/xfer/post")
    def xfer_post(request: Request, x_fr: str = Form(""), x_to: str = Form(""), x_amt: str = Form("")):
        if not session_ok(request):
            return expired()
        state.posted_transfers.append({"from": x_fr, "to": x_to, "amount": x_amt})
        return page("TRANSFER POSTED", f"CONFIRMATION NO. TX{len(state.posted_transfers):06d}")

    return app


app = create_app()
