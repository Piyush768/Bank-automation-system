"""End-to-end demonstration that writes the curated /evidence folder.

    goal -> REAL LLM discovery -> capability artifact -> deterministic replays
    (success, business outcomes, recoverable conditions, hard failures,
    human escalation on the live session, second tenant via overlay)

Discovery needs a real model key (.env). Replays never call a model.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from cua import runtime
from cua.escalation.console import ScriptedOperator
from cua.schema.artifact import Capability
from mock_bank.server import AppServer

ROOT = runtime.ROOT
EVIDENCE = ROOT / "evidence"
CAP_PATH = ROOT / "capabilities" / "member-savings-balance.json"
GOAL = "Look up member 10492 and read their current primary savings balance"
XFER_PATH = ROOT / "capabilities" / "funds-transfer.json"
XFER_GOAL = ("Transfer 25.00 from account 10492-S01 to account 10492-D10, post it, "
             "and return the confirmation number shown after posting")
XFER_PARAMS = {"from_account": "10492-S01", "to_account": "10492-D10", "amount": "25.00"}


def _scenario(name: str) -> str:
    d = EVIDENCE / name
    if d.exists():
        shutil.rmtree(d)
    return str(d)


def run_demo(skip_discovery: bool = False, provider=None) -> list[dict]:
    runtime.load_env()
    os.makedirs(ROOT / "capabilities", exist_ok=True)
    EVIDENCE.mkdir(exist_ok=True)
    rows: list[dict] = []
    tenant_a = str(ROOT / "config" / "tenants" / "harbor-point.json")
    tenant_b = str(ROOT / "config" / "tenants" / "lakeshore.json")

    with AppServer(8600, "a") as app, AppServer(8601, "b") as app_b:
        # ------------------------------------------------------ 1. discovery
        if not skip_discovery:
            if provider is not None and provider.name == "scripted-test" and EVIDENCE == ROOT / "evidence":
                raise RuntimeError("refusing to write /evidence from the test-only scripted provider")
            print("\n== 01 discovery (live LLM) ==")
            cap, summary = runtime.discover(
                goal=GOAL, start_url=app.base_url + "/signon", params={"member_id": "10492"},
                capability_id="member-savings-balance", title="Member primary savings balance inquiry",
                out_path=str(CAP_PATH), evidence_dir=_scenario("01-discovery"), provider=provider,
                tenant_id="harbor-point-cu",
            )
            print(json.dumps(summary, indent=2))
            rows.append({"scenario": "01-discovery", "status": summary["status"], "detail":
                         f"{summary['llm_calls']} LLM calls, {summary['steps']} steps, outputs={summary['outputs']}"})
            if cap is None:
                raise SystemExit(f"discovery did not produce an artifact: {summary.get('compile_error') or summary['summary']}")
        cap = Capability.load(str(CAP_PATH))
        shutil.copy(CAP_PATH, EVIDENCE / "member-savings-balance.capability.json")

        def replay(name, params, *, tenant=tenant_a, faults=None, operator="none", scripted=None, note="",
                   capability=None, count_transfers=False):
            print(f"\n== {name} ==")
            app.reset()
            app_b.reset()
            if faults:
                (app_b if tenant == tenant_b else app).faults(**faults)
            res = runtime.replay(capability=capability or cap, params=params, tenant_path=tenant, base_url=None,
                                 evidence_dir=_scenario(name), operator=operator, scripted=scripted,
                                 allow_draft=True, step_timeout_s=6)
            detail = res.outputs or (res.outcome and res.outcome.code) or (res.failure and f"{res.failure.code}: {res.failure.message}")
            extras = []
            if res.recoveries:
                extras.append("recoveries=" + ",".join(r.code for r in res.recoveries))
            if res.escalations:
                extras.append("escalations=" + ",".join(f"{e.kind}->{e.resolution}" for e in res.escalations))
            if count_transfers:
                extras.append(f"transfers_posted={len(app.transfers())}")
            degraded = [l.step_id for l in res.locators if l.degraded]
            if degraded:
                extras.append("degraded_locators=" + ",".join(degraded))
            print(res.status, detail, *extras)
            rows.append({"scenario": name, "status": res.status, "detail": f"{detail} {' '.join(extras)}".strip(), "note": note})
            return res

        # ------------------------------------------------------ 2. replays
        replay("02-replay-success", {"member_id": "10492"}, note="happy path")
        replay("03-replay-success-other-row-order", {"member_id": "20318"},
               note="savings is row 2 for this member: content-addressed cell, not position")
        replay("04-replay-business-not-found", {"member_id": "99999"}, note="E-404 -> MEMBER_NOT_FOUND")
        replay("05-replay-business-access-denied", {"member_id": "10007"}, note="E-403 -> ACCESS_DENIED")
        replay("06-replay-input-rejected", {"member_id": "12AB"}, note="rejected before touching the UI")
        replay("07-replay-recoverable-conditions", {"member_id": "10492"},
               faults={"interstitial_once": True, "expire_once": True, "slow_ms": 700},
               note="notice dismissed, session expiry -> restart, slow host")
        replay("08-replay-hard-failure-host-abend", {"member_id": "10492"}, faults={"errors_remaining": 5},
               note="ABEND persists after one restart -> APP_ERROR with screenshot + DOM")
        op = ScriptedOperator("", "operator-jdoe",
                              actions=[("fill_label", {"label": "OVERRIDE CODE", "value": "4242"}),
                                       ("click_text", {"text": "ACKNOWLEDGE"})],
                              handback=("resume_verify", "W-77 supervisor override entered; member cleared"))
        replay("09-replay-escalation-live-handoff", {"member_id": "10492"}, faults={"supervisor_once": True},
               operator="scripted", scripted=op,
               note="unknown screen -> operator takes the live session -> hands back -> verified -> outputs")
        replay("10-replay-tenant-b-overlay", {"member_id": "20318"}, tenant=tenant_b,
               note="same artifact on another institution via text overlay")
        no_overlay = str(ROOT / "config" / "tenants" / "lakeshore-no-overlay.json")
        with open(tenant_b) as f:
            t = json.load(f)
        t["text_overrides"] = {}
        with open(no_overlay, "w") as f:
            json.dump(t, f, indent=2)
        try:
            replay("11-replay-hard-failure-drift", {"member_id": "20318"}, tenant=no_overlay,
                   note="tenant B WITHOUT overlay: fallbacks keep going (drift flagged) but the checkpoint refuses the wrong screen")
        finally:
            os.remove(no_overlay)

        # ------------------------------------ 3. irreversible flow under dual control
        if not skip_discovery:
            print("\n== 12 discovery: funds transfer (live LLM, human approval) ==")
            app.reset()
            approver = ScriptedOperator("", "supervisor-mlee", [],
                                        ("approve", "Dual control: accounts and amount verified"), wait_s=900)
            xcap, xsum = runtime.discover(
                goal=XFER_GOAL, start_url=app.base_url + "/signon", params=XFER_PARAMS,
                capability_id="funds-transfer", title="Internal funds transfer (dual control)",
                out_path=str(XFER_PATH), evidence_dir=_scenario("12-discovery-transfer-dual-control"),
                provider=provider, tenant_id="harbor-point-cu", operator="scripted", scripted=approver,
            )
            print(json.dumps(xsum, indent=2))
            rows.append({"scenario": "12-discovery-transfer-dual-control", "status": xsum["status"],
                         "detail": f"{xsum['llm_calls']} LLM calls, outputs={xsum['outputs']}, "
                                   f"transfers_posted={len(app.transfers())}",
                         "note": "live discovery of an irreversible flow: POST TRANSFER paused for a human approver"})
        if XFER_PATH.exists():
            xcap = Capability.load(str(XFER_PATH))
            shutil.copy(XFER_PATH, EVIDENCE / "funds-transfer.capability.json")
            ok = ScriptedOperator("", "supervisor-mlee", [], ("approve", "Dual control: verified"))
            replay("13-replay-transfer-approved", XFER_PARAMS, capability=xcap, operator="scripted", scripted=ok,
                   count_transfers=True, note="irreversible step approved by a human: posted exactly once")
            no = ScriptedOperator("", "supervisor-mlee", [], ("deny", "Amount not authorised for this member"))
            replay("14-replay-transfer-denied", XFER_PARAMS, capability=xcap, operator="scripted", scripted=no,
                   count_transfers=True, note="approver denies: APPROVAL_DENIED, nothing posted")
            replay("15-replay-transfer-no-approver", XFER_PARAMS, capability=xcap, count_transfers=True,
                   note="no operator attached: fails closed (POLICY_BLOCKED), nothing posted")

    _write_index(rows)
    return rows


def _write_index(rows: list[dict]) -> None:
    lines = ["# Evidence index", "", "Generated by `cua demo`. Each folder has `events.jsonl` (redacted, one event per line),",
             "`screens/` (PII-masked), `dom/` (redacted, on failure/escalation) and `result.json`.", "",
             "| Scenario | Status | Detail | What it shows |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['scenario']}` | {r['status']} | {str(r['detail']).replace('|', '/')} | {r.get('note', '')} |")
    lines += ["", "## Evidence portability", "",
              "Paths inside `result.json` files are repository-relative, so the evidence does not depend on the",
              "machine where it was captured. Credentials and operator-entered values appear only as placeholders",
              "or redacted values; screenshots are PII-masked before they are written."]
    (EVIDENCE / "README.md").write_text("\n".join(lines) + "\n")
