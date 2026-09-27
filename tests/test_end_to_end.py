"""Integration tests against the live mock app in a real (headless) browser.

Discovery here uses the TEST-ONLY ScriptedProvider so the suite runs offline.
The genuine LLM discovery run is `cua demo` / `cua discover` with a key.
"""

import json
import os

import httpx
import pytest

from cua import runtime
from cua.agent.llm import ScriptedProvider
from cua.escalation.console import ScriptedOperator
from cua.schema.artifact import Capability
from cua.tenancy import Tenant, specialise
from mock_bank.server import AppServer
from tests.plans import ESCALATE_PLAN, SAVINGS_PLAN, TRANSFER_PLAN

os.environ.setdefault("BANK_USERNAME", "teller01")
os.environ.setdefault("BANK_PASSWORD", "demo-pass-123")

PORT_A, PORT_B = 8620, 8621
GOAL = "Look up member 10492 and read their primary savings balance"


@pytest.fixture(scope="module")
def apps():
    a, b = AppServer(PORT_A, "a").start(), AppServer(PORT_B, "b").start()
    yield a, b
    a.stop()
    b.stop()


@pytest.fixture(autouse=True)
def _reset(apps):
    for app in apps:
        app.reset()


@pytest.fixture(scope="module")
def savings_cap(apps, tmp_path_factory):
    d = tmp_path_factory.mktemp("disc")
    cap, summary = runtime.discover(
        goal=GOAL, start_url=apps[0].base_url + "/signon", params={"member_id": "10492"},
        capability_id="member-savings-balance", title="Savings", out_path=str(d / "cap.json"),
        evidence_dir=str(d / "ev"), provider=ScriptedProvider(SAVINGS_PLAN),
    )
    assert cap is not None, summary
    return cap


def _replay(cap, params, tmp_path, base=f"http://127.0.0.1:{PORT_A}", **kw):
    kw.setdefault("allow_draft", True)
    kw.setdefault("step_timeout_s", 3)
    return runtime.replay(capability=cap, params=params, tenant_path=kw.pop("tenant", None), base_url=base,
                          evidence_dir=str(tmp_path / "ev"), **kw)


# ------------------------------------------------------------- artifact

def test_artifact_is_parameterised_and_holds_no_data(savings_cap):
    blob = savings_cap.to_json()
    assert "{{params.member_id}}" in blob and "{{secrets.password}}" in blob
    for leaked in ("10492", "demo-pass-123", "teller01", "14,250.80", "521-44-9087", "WINTERS"):
        assert leaked not in blob.split('"provenance"')[0], leaked
    assert savings_cap.inputs[0].pattern == r"^\d{5}$"
    assert savings_cap.review.status == "draft"
    assert savings_cap.compute_hash() == savings_cap.content_hash
    # the member-name link is addressed by the parameterised row, not by the name
    link = next(s for s in savings_cap.steps if s.target and s.target.strategies[0].kind == "table_cell")
    assert link.target.strategies[0].row_contains == "{{params.member_id}}"


def test_every_recorded_step_has_multiple_validated_strategies(savings_cap):
    for s in savings_cap.steps:
        if s.target:
            assert len(s.target.strategies) >= 2, s.id
            assert s.target.strategies[-1].kind == "coords"  # always last


# --------------------------------------------------------------- replay

def test_replay_success(savings_cap, tmp_path):
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path)
    assert r.status == "success" and r.outputs == {"savings_balance": "14250.80"}
    assert not any(l.degraded for l in r.locators)


def test_replay_reads_by_content_not_position(savings_cap, tmp_path):
    r = _replay(savings_cap, {"member_id": "20318"}, tmp_path)  # savings is row 2 here
    assert r.outputs == {"savings_balance": "3402.17"}


@pytest.mark.parametrize("mid,code", [("99999", "MEMBER_NOT_FOUND"), ("10007", "ACCESS_DENIED")])
def test_business_outcomes_are_not_failures(savings_cap, tmp_path, mid, code):
    r = _replay(savings_cap, {"member_id": mid}, tmp_path)
    assert r.status == "business_outcome" and r.outcome.code == code and r.failure is None


def test_bad_input_rejected_before_ui(savings_cap, tmp_path):
    r = _replay(savings_cap, {"member_id": "12AB"}, tmp_path)
    assert r.status == "failed" and r.failure.code == "INPUT_INVALID" and r.failure.screenshot is None


def test_recoverable_conditions_handled_in_run(savings_cap, apps, tmp_path):
    apps[0].faults(interstitial_once=True, expire_once=True, slow_ms=400)
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path)
    assert r.status == "success"
    assert [x.code for x in r.recoveries] == ["SYSTEM_NOTICE", "SESSION_EXPIRED"]


def test_transient_abend_recovers_once_then_hard_fails(savings_cap, apps, tmp_path):
    apps[0].faults(errors_remaining=1)
    assert _replay(savings_cap, {"member_id": "10492"}, tmp_path / "a").status == "success"
    apps[0].faults(errors_remaining=5)
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path / "b")
    assert r.status == "failed" and r.failure.code == "APP_ERROR" and r.failure.retryable
    assert r.failure.screenshot and r.failure.dom_snapshot


def test_unknown_screen_without_operator_is_debuggable_failure(savings_cap, apps, tmp_path):
    apps[0].faults(supervisor_once=True)
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path)
    f = r.failure
    assert r.status == "failed" and f.code == "CHECKPOINT_FAILED"
    assert f.at_step and f.expected and f.observed["landmarks"] and f.screenshot and f.dom_snapshot
    assert "SUPERVISOR OVERRIDE REQUIRED" in f.observed["landmarks"]


def test_escalation_hands_live_session_to_operator_and_resumes(savings_cap, apps, tmp_path):
    apps[0].faults(supervisor_once=True)
    op = ScriptedOperator("", "op-1", [("fill_label", {"label": "OVERRIDE CODE", "value": "4242"}),
                                       ("click_text", {"text": "ACKNOWLEDGE"})], ("resume_verify", "override entered"))
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path, operator="scripted", scripted=op)
    assert r.status == "success" and r.outputs["savings_balance"] == "14250.80"
    assert r.human_assisted and r.escalations[0].operator == "op-1"
    acts = r.escalations[0].operator_actions
    assert any(a.get("action") == "fill" and "4242" not in json.dumps(a) for a in acts)  # value never recorded
    events = (tmp_path / "ev" / "events.jsonl").read_text()
    assert "control_claimed" in events and "control_returned" in events and "4242" not in events


def test_automation_cannot_act_while_operator_holds_session():
    from cua.escalation.control import ControlChannel
    from cua.surface.browser import ControlViolation

    ch = ControlChannel()
    ch.request("stuck", "test", {}, None)
    with pytest.raises(ControlViolation):
        ch.assert_can_act("automation")
    ch.claim("op")
    ch.assert_can_act("operator")
    with pytest.raises(ControlViolation):
        ch.assert_can_act("automation")
    ch.close_intervention("resolved", "resume_verify", "")
    ch.assert_can_act("automation")


def test_tenant_overlay_reuses_artifact(savings_cap, tmp_path):
    tenant_path = tmp_path / "t.json"
    t = json.load(open(runtime.ROOT / "config" / "tenants" / "lakeshore.json"))
    t["base_url"] = f"http://127.0.0.1:{PORT_B}"
    tenant_path.write_text(json.dumps(t))
    spec, changes = specialise(savings_cap, Tenant.load(str(tenant_path)))
    assert changes and spec.content_hash == savings_cap.content_hash
    r = _replay(savings_cap, {"member_id": "20318"}, tmp_path, base=None, tenant=str(tenant_path))
    assert r.status == "success" and r.outputs == {"savings_balance": "3402.17"}


def test_drift_without_overlay_is_flagged_and_refused(savings_cap, tmp_path):
    r = _replay(savings_cap, {"member_id": "20318"}, tmp_path, base=f"http://127.0.0.1:{PORT_B}")
    assert r.status == "failed"
    assert any(l.degraded for l in r.locators)  # drift signal recorded


# --------------------------------------------------- approval & integrity

def test_draft_needs_approval_and_approval_binds_to_hash(savings_cap, tmp_path):
    r = _replay(savings_cap, {"member_id": "10492"}, tmp_path / "a", allow_draft=False)
    assert r.failure.code == "NOT_APPROVED"
    cap = savings_cap.model_copy(deep=True)
    cap.review.status, cap.review.approved_hash = "approved", cap.content_hash
    assert _replay(cap, {"member_id": "10492"}, tmp_path / "b", allow_draft=False).status == "success"
    cap.steps[1].intent = "tampered"  # any edit voids integrity
    assert _replay(cap, {"member_id": "10492"}, tmp_path / "c", allow_draft=False).failure.code == "NOT_APPROVED"


def test_network_allowlist_blocks_off_list_requests(tmp_path):
    from cua.safety.policy import Policy
    from cua.safety.redact import Redactor
    from cua.surface.browser import BrowserSurface

    s = BrowserSurface(Policy.load(str(runtime.POLICY)), Redactor())
    try:
        s.page.set_content('<img src="https://tracker.example.com/x.gif">')
        s.page.wait_for_timeout(300)
        assert any("tracker.example.com" in b["url"] for b in s.blocked)
        with pytest.raises(PermissionError):
            s.navigate("https://evil.example.com/")
    finally:
        s.close()


# ------------------------------------------ discovery escalation & risky steps

def test_discovery_escalation_marks_artifact_operator_assisted(apps, tmp_path):
    op = ScriptedOperator("", "op-2", [], ("resume_verify", "sign on with the USER ID and PASSWORD fields"))
    cap, summary = runtime.discover(
        goal=GOAL, start_url=apps[0].base_url + "/signon", params={"member_id": "10492"},
        capability_id="x", title="x", out_path=str(tmp_path / "c.json"), evidence_dir=str(tmp_path / "ev"),
        provider=ScriptedProvider(ESCALATE_PLAN), operator="scripted", scripted=op,
    )
    assert cap and cap.provenance.operator_assisted and "NOT compiled" in cap.review.notes


@pytest.fixture(scope="module")
def transfer_cap(apps, tmp_path_factory):
    d = tmp_path_factory.mktemp("xfer")
    op = ScriptedOperator("", "supervisor-1", [], ("approve", "dual control ok"))
    cap, summary = runtime.discover(
        goal="Move 25.00 from 10492-S01 to 10492-D10", start_url=apps[0].base_url + "/signon",
        params={"from_account": "10492-S01", "to_account": "10492-D10", "amount": "25.00"},
        capability_id="funds-transfer", title="Funds transfer", out_path=str(d / "c.json"),
        evidence_dir=str(d / "ev"), provider=ScriptedProvider(TRANSFER_PLAN), operator="scripted", scripted=op,
    )
    assert cap is not None, summary
    return cap


def test_irreversible_step_recorded_as_such(transfer_cap):
    post = next(s for s in transfer_cap.steps if s.risk == "irreversible")
    assert "POST TRANSFER" in json.dumps(post.target.model_dump())
    assert transfer_cap.guardrails.max_risk == "irreversible"


def _transfers(apps):
    return httpx.get(apps[0].base_url + "/_harness/transfers").json()


def test_irreversible_replay_blocked_without_operator(transfer_cap, apps, tmp_path):
    p = {"from_account": "10492-S01", "to_account": "10492-D10", "amount": "25.00"}
    r = _replay(transfer_cap, p, tmp_path)
    assert r.failure.code == "POLICY_BLOCKED" and _transfers(apps) == []


def test_irreversible_replay_denied_by_approver(transfer_cap, apps, tmp_path):
    p = {"from_account": "10492-S01", "to_account": "10492-D10", "amount": "25.00"}
    op = ScriptedOperator("", "supervisor-1", [], ("deny", "amount not authorised"))
    r = _replay(transfer_cap, p, tmp_path, operator="scripted", scripted=op)
    assert r.failure.code == "APPROVAL_DENIED" and _transfers(apps) == []


def test_irreversible_replay_approved_posts_once(transfer_cap, apps, tmp_path):
    p = {"from_account": "10492-S01", "to_account": "10492-D10", "amount": "25.00"}
    op = ScriptedOperator("", "supervisor-1", [], ("approve", "dual control ok"))
    r = _replay(transfer_cap, p, tmp_path, operator="scripted", scripted=op)
    assert r.status == "success" and r.outputs["confirmation"].startswith("CONFIRMATION NO.")
    assert _transfers(apps) == [{"from": "10492-S01", "to": "10492-D10", "amount": "25.00"}]
