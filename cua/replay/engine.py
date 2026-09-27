"""Deterministic replay: the production execution path. No LLM anywhere.

For every step:

1. **Resolve** the target by trying its ranked locator strategies. Each must
   match exactly one element of the expected control kind. While waiting, the
   page is checked against the app's condition catalog *first*, so a missing
   button caused by "E-404 NO MEMBER" is reported as that business outcome,
   not as a locator failure.
2. **Guard**: re-classify risk from the live control text (never trust the
   artifact alone); irreversible steps need a single-use human approval.
3. **Act**, then **wait for evidence**: poll until the step's checkpoints hold
   OR a known condition appears OR the step deadline passes. Waiting is
   condition-driven, never a fixed sleep.
4. Unknown state at the deadline -> escalate to a human on the same live
   session (if a console is attached) or fail with a debuggable report.

Condition classes from the profile map onto the result contract:
business -> terminal ``business_outcome``; recoverable -> handled in-run and
listed in ``recoveries``; fatal -> terminal ``failed``.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from cua.escalation.control import ControlChannel
from cua.escalation.handoff import hold_for_operator
from cua.evidence import Evidence
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.schema.artifact import Capability, Checkpoint, Step, Target
from cua.schema.results import (
    BusinessOutcome,
    EscalationRecord,
    Failure,
    LocatorUse,
    Recovery,
    ReplayResult,
)
from cua.surface.browser import BrowserSurface
from cua.tenancy import AppProfile, Condition

TEMPLATE_RE = re.compile(r"\{\{(params|secrets)\.([a-z_][a-z0-9_]*)\}\}")


class _Terminal(Exception):
    def __init__(self, status: str, failure: Failure | None = None, outcome: BusinessOutcome | None = None):
        self.status, self.failure, self.outcome = status, failure, outcome


class _Restart(Exception):
    def __init__(self, code: str, at_step: str):
        self.code, self.at_step = code, at_step


class ReplayEngine:
    def __init__(
        self,
        surface: BrowserSurface,
        profile: AppProfile,
        policy: Policy,
        redactor: Redactor,
        evidence: Evidence,
        channel: ControlChannel | None = None,
        step_timeout_s: float = 8.0,
        operator_timeout_s: float = 900,
        allow_draft: bool = False,
    ):
        self.s, self.profile, self.policy, self.red, self.ev = surface, profile, policy, redactor, evidence
        self.channel = channel
        self.step_timeout_s = step_timeout_s
        self.operator_timeout_s = operator_timeout_s
        self.allow_draft = allow_draft

    # ================================================================ public

    def run(self, cap: Capability, params: dict[str, str], secrets: dict[str, str], base_url: str,
            canonical: Capability | None = None) -> ReplayResult:
        """`cap` may be a tenant-specialised copy; integrity and approval are
        always checked on the `canonical` artifact it was derived from."""
        self._t0 = time.time()
        self._cap = cap
        self._canonical = canonical or cap
        self._params, self._secrets, self._base = params, secrets, base_url.rstrip("/")
        for v in secrets.values():
            self.red.add_secret(v)
        self._result = ReplayResult(
            run_id=self.ev.run_id, capability_id=cap.id, capability_version=cap.version,
            capability_hash=cap.content_hash, status="failed", evidence_dir=self.ev.dir,
        )
        self.ev.event("replay_start", capability=cap.id, version=cap.version, hash=cap.content_hash,
                      review=cap.review.status, params=params, base_url=base_url)
        try:
            self._preflight()
            self._execute()
            self._result.status = "success"
            self.ev.screenshot(self.s, "final-success")
        except _Terminal as t:
            self._result.status = t.status
            self._result.failure = t.failure
            self._result.outcome = t.outcome
        except Exception as e:  # never leak a raw traceback to the caller
            self._result.status = "failed"
            self._result.failure = Failure(code="INTERNAL_ERROR", message=f"{type(e).__name__}: {str(e)[:300]}",
                                           url=self._url(), screenshot=self.ev.screenshot(self.s, "internal-error"),
                                           dom_snapshot=self.ev.dom(self.s, "internal-error"))
        self._result.duration_ms = int((time.time() - self._t0) * 1000)
        self._result.human_assisted = bool(self._result.escalations)
        self.ev.event("replay_end", **self._result.model_dump(mode="json", exclude={"evidence_dir"}))
        self.ev.write_json("result.json", self._result.model_dump(mode="json"))
        return self._result

    # ============================================================= preflight

    def _preflight(self) -> None:
        cap, canon = self._cap, self._canonical
        if canon.compute_hash() != canon.content_hash:
            self._fail("NOT_APPROVED", "artifact content does not match its content_hash (edited after compile)")
        approved = canon.review.status == "approved" and canon.review.approved_hash == canon.content_hash
        if not approved and not self.allow_draft:
            self._fail("NOT_APPROVED", f"capability review status is '{canon.review.status}'; unattended replay needs approval")
        if not approved:
            self.ev.event("draft_replay", note="running a draft capability because allow_draft is set")
        ok, reason = self.policy.url_allowed(self._base + cap.entry_path)
        if not ok:
            self._fail("POLICY_BLOCKED", f"tenant base URL not allowed: {reason}")
        for st in cap.steps:
            if st.action not in self.policy.allowed_actions:
                self._fail("POLICY_BLOCKED", f"step {st.id} uses action '{st.action}' not in policy allowlist", st)
        for p in cap.inputs:
            v = self._params.get(p.name)
            if v is None:
                self._fail("INPUT_INVALID", f"missing required input '{p.name}'", expected=p.model_dump())
            if p.pattern and not re.fullmatch(p.pattern, str(v)):
                self._fail("INPUT_INVALID", f"input '{p.name}' does not match {p.pattern}",
                           expected=p.pattern, observed=v)
        extra = set(self._params) - {p.name for p in cap.inputs}
        if extra:
            self._fail("INPUT_INVALID", f"unexpected inputs: {sorted(extra)}")
        for s in cap.secrets:
            if not self._secrets.get(s.name):
                self._fail("INPUT_INVALID", f"secret '{s.name}' not provided by the runtime secret store")

    # ============================================================== execute

    def _execute(self) -> None:
        budgets: dict[str, int] = {}
        while True:
            self._irreversible_done = False
            try:
                for step in self._cap.steps:
                    self._run_step(step)
                self._verify_success()
                self._extract_outputs()
                return
            except _Restart as r:
                budgets[r.code] = budgets.get(r.code, 0) + 1
                cond = next(c for c in self.profile.conditions if c.code == r.code)
                if budgets[r.code] > int(cond.recovery.get("max", 1)):
                    self._fail("RECOVERY_EXHAUSTED" if r.code != "HOST_ABEND" else "APP_ERROR",
                               f"{r.code} persisted after {budgets[r.code] - 1} restart(s): {cond.message}",
                               retryable=True)
                self._result.recoveries.append(Recovery(code=r.code, at_step=r.at_step, action="restart flow from sign-on"))
                self.ev.event("restart_flow", reason=r.code, attempt=budgets[r.code])

    def _run_step(self, step: Step) -> None:
        self.ev.event("step_start", step=step.id, action=step.action, intent=step.intent, risk=step.risk)
        if step.action == "navigate":
            self.s.navigate(self._base + self._render(step.url_path or "/"))
            self._await_checkpoints(step)
            return

        target = self._render_target(step.target)
        handle = self._resolve(step, target, purpose="act")
        if handle is _SKIP_ACT:
            self._await_checkpoints(step)
            return

        # Re-classify risk from what the live control actually says.
        live_text = self._safe_text(handle) if step.action in ("click", "press") else ""
        risk = max(step.risk, self.policy.classify(step.action, live_text), key=["safe", "reversible", "irreversible"].index)
        verdict = self.policy.check_action(step.action, risk)
        if verdict.verdict == "block":
            self._fail("POLICY_BLOCKED", verdict.reason, step)
        if verdict.verdict == "approve":
            self._approval(step, live_text or step.target.description)
            handle = self._resolve(step, target, purpose="act")  # page may have been refreshed
        value = self._render(step.value) if step.value is not None else None
        self.s.act(handle, step.action, value)
        if risk == "irreversible":
            self._irreversible_done = True
        self.ev.event("action", step=step.id, action=step.action, value=step.value, risk=risk)
        self._await_checkpoints(step)

    # ------------------------------------------------------------ resolution

    def _resolve(self, step: Step, target: Target, purpose: str):
        escalations = 0
        while True:
            deadline = time.time() + self.step_timeout_s
            while time.time() < deadline:
                self._check_conditions(step)
                r = self.s.resolve(target)
                if r.handle is not None:
                    use = LocatorUse(step_id=step.id if purpose == "act" else f"output:{purpose}",
                                     strategy=r.strategy, rank=r.rank, degraded=r.rank > 0, misses=r.misses)
                    self._result.locators.append(use)
                    self.ev.event("resolved", step=use.step_id, strategy=r.strategy, rank=r.rank, misses=r.misses,
                                  degraded=use.degraded)
                    return r.handle
                self.s.pump(250)
            misses = self.s.resolve(target).misses
            mode = self._stuck(step, "LOCATOR_NOT_FOUND", f"could not find {target.description}",
                               expected={"target": target.description, "strategies": [m.kind for m in target.strategies]},
                               observed={"misses": misses, "landmarks": self.s.landmarks()}, escalations=escalations)
            escalations += 1
            if mode == "step_done":
                return _SKIP_ACT
            # resume_verify / retry_step: look for the target again with a fresh deadline

    # ------------------------------------------------------------ checkpoints

    def _checkpoint_ok(self, cp: Checkpoint) -> bool:
        v = self._render(cp.value)
        if cp.kind == "url_path":
            return self.s.current_url().split("?", 1)[0].endswith(v.split("?", 1)[0])
        return _norm(v) in _norm(self.s.page_text())

    def _await_checkpoints(self, step: Step, checkpoints: list[Checkpoint] | None = None) -> None:
        cps = step.expect if checkpoints is None else checkpoints
        if not cps:
            self._check_conditions(step)
            return
        escalations = 0
        while True:
            deadline = time.time() + self.step_timeout_s
            while time.time() < deadline:
                self._check_conditions(step)
                if all(self._checkpoint_ok(c) for c in cps):
                    self.ev.event("checkpoint_ok", step=step.id, checks=[self._render(c.value) for c in cps])
                    return
                self.s.pump(200)
            mode = self._stuck(step, "CHECKPOINT_FAILED", "expected state not reached",
                               expected=[{"kind": c.kind, "value": self._render(c.value)} for c in cps],
                               observed={"url": self._url(), "landmarks": self.s.landmarks()}, escalations=escalations)
            escalations += 1
            if mode == "retry_step":
                self._run_step(step)
                return
            # resume_verify / step_done: verify, don't trust -> loop and re-check

    def _verify_success(self) -> None:
        pseudo = Step(id="success", action="click", intent="verify capability success condition")
        self._await_checkpoints(pseudo, self._cap.success)

    # ------------------------------------------------------------- conditions

    def _check_conditions(self, step: Step) -> None:
        cond = self.profile.detect(self.s.page_text())
        if cond:
            self._handle_condition(cond, step)

    def _handle_condition(self, cond: Condition, step: Step) -> None:
        self.ev.event("condition_detected", step=step.id, code=cond.code, cls=cond.cls, message=cond.message)
        if cond.cls == "business":
            self.ev.screenshot(self.s, f"outcome-{cond.code}")
            raise _Terminal("business_outcome", outcome=BusinessOutcome(code=cond.code, message=cond.message, at_step=step.id))
        if cond.cls == "fatal":
            self._fail("SIGNON_REJECTED" if cond.code == "SIGNON_REJECTED" else "APP_ERROR", cond.message, step)
        rtype = cond.recovery.get("type")
        if rtype == "click_text":
            if self.s.click_text(cond.recovery["text"]):
                self._result.recoveries.append(Recovery(code=cond.code, at_step=step.id, action=f"clicked {cond.recovery['text']}"))
                self.ev.event("recovery", code=cond.code, action="click_text", text=cond.recovery["text"])
                return
            self._fail("APP_ERROR", f"{cond.code}: recovery control '{cond.recovery['text']}' not found", step)
        if rtype == "restart_flow":
            if self._irreversible_done:
                # Never re-run a flow that already moved money: a human must reconcile.
                self._stuck(step, "RECOVERY_EXHAUSTED", f"{cond.code} after an irreversible step; restart is unsafe",
                            expected="safe restart", observed=cond.code, escalations=1, allow_resume=False)
            self.ev.screenshot(self.s, f"recovery-{cond.code}")
            raise _Restart(cond.code, step.id)
        self._fail("APP_ERROR", f"{cond.code}: no recovery defined", step)

    # ---------------------------------------------------------------- outputs

    def _extract_outputs(self) -> None:
        pseudo = Step(id="outputs", action="click", intent="extract declared outputs")
        for out in self._cap.outputs:
            handle = self._resolve(pseudo, self._render_target(out.source), purpose=out.name)
            if handle is _SKIP_ACT:
                self._fail("OUTPUT_PARSE_ERROR", f"output '{out.name}' was skipped by the operator")
            raw = self.s.read(handle)
            try:
                self._result.outputs[out.name] = parse_value(raw, out.type)
            except ValueError as e:
                self._fail("OUTPUT_PARSE_ERROR", f"output '{out.name}': {e}", pseudo,
                           expected=out.type, observed=self.red.text(raw))
        self.ev.event("outputs", outputs=self._result.outputs)

    # ------------------------------------------------------ escalation / fail

    def _stuck(self, step: Step, code: str, message: str, expected, observed, escalations: int,
               allow_resume: bool = True) -> str:
        shot = self.ev.screenshot(self.s, f"stuck-{step.id}")
        dom = self.ev.dom(self.s, f"stuck-{step.id}")
        cond_hint = self.profile.detect(self.s.page_text())
        if not self.channel or escalations >= 2:
            self._fail(code, message, step, expected=expected, observed=observed, shot=shot, dom=dom)
        iv = self.channel.request(
            "unknown_state" if allow_resume else "unrecoverable", f"{code}: {message}",
            {"capability": self._cap.id, "step": step.id, "intent": step.intent, "expected": expected,
             "observed": observed, "url": self._url(), "known_condition": cond_hint.code if cond_hint else None},
            shot,
        )
        mode, note = hold_for_operator(self.channel, self.s, self.ev, iv, self.operator_timeout_s)
        self._result.escalations.append(EscalationRecord(
            intervention_id=iv.id, kind=iv.kind, reason=iv.reason, at_step=step.id, resolution=mode,
            operator=iv.operator, operator_actions=iv.operator_actions,
        ))
        if note == "operator timeout":
            raise _Terminal("escalated", failure=Failure(code=code, message=f"{message}; awaiting operator (timed out)",
                                                         at_step=step.id, screenshot=shot, dom_snapshot=dom, retryable=True))
        if mode == "abort" or not allow_resume:
            self._fail("HUMAN_ABORTED" if mode == "abort" else code, f"{message}; operator note: {note}", step,
                       expected=expected, observed=observed, shot=shot, dom=dom)
        return mode

    def _approval(self, step: Step, control_text: str) -> None:
        shot = self.ev.screenshot(self.s, f"approval-{step.id}")
        if not self.channel:
            self._fail("POLICY_BLOCKED", "irreversible step requires human approval and no operator channel is attached", step)
        iv = self.channel.request("approval", f"irreversible step {step.id}: '{self.red.text(control_text)}'",
                                  {"capability": self._cap.id, "step": step.id, "intent": step.intent,
                                   "params": self.red.obj(self._params)}, shot)
        mode, note = hold_for_operator(self.channel, self.s, self.ev, iv, self.operator_timeout_s)
        self._result.escalations.append(EscalationRecord(
            intervention_id=iv.id, kind="approval", reason=iv.reason, at_step=step.id, resolution=mode,
            operator=iv.operator, operator_actions=iv.operator_actions))
        if mode != "approve":
            self._fail("APPROVAL_DENIED", f"approver chose '{mode}': {note}", step)

    def _fail(self, code: str, message: str, step: Step | None = None, expected=None, observed=None,
              shot: str | None = None, dom: str | None = None, retryable: bool = False):
        on_page = code not in ("INPUT_INVALID", "NOT_APPROVED") and self.s.page.url != "about:blank"
        if on_page and shot is None:
            shot = self.ev.screenshot(self.s, f"fail-{code}")
            dom = self.ev.dom(self.s, f"fail-{code}")
        raise _Terminal("failed", failure=Failure(
            code=code, message=self.red.text(message), at_step=step.id if step else None,
            step_intent=step.intent if step else None, expected=self.red.obj(expected), observed=self.red.obj(observed),
            url=self.red.text(self._url()) if on_page else None, screenshot=shot, dom_snapshot=dom, retryable=retryable,
        ))

    # ---------------------------------------------------------------- utils

    def _render(self, s: str | None) -> str | None:
        if s is None:
            return None

        def sub(m: re.Match) -> str:
            src = self._params if m.group(1) == "params" else self._secrets
            return str(src[m.group(2)])

        return TEMPLATE_RE.sub(sub, s)

    def _render_target(self, t: Target) -> Target:
        def walk(o):
            if isinstance(o, str):
                return self._render(o)
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(v) for v in o]
            return o

        return Target.model_validate(walk(t.model_dump()))

    def _safe_text(self, handle) -> str:
        try:
            return handle.evaluate("e => (e.value || e.innerText || '').trim()")
        except Exception:
            return ""

    def _url(self) -> str:
        try:
            return self.s.current_url()
        except Exception:
            return ""


_SKIP_ACT = object()


def _norm(t: str | None) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def parse_value(raw: str, typ: str):
    """Typed output parsing. Money is returned as a Decimal-string, never a float."""
    s = raw.strip()
    if typ == "currency":
        # Accounting negatives: "(8,120.00)", "$(8,120.00)", "-8,120.00", "8,120.00-"
        core = s.replace("$", "").replace(" ", "")
        neg = (core.startswith("(") and core.endswith(")")) or core.startswith("-") or core.endswith("-")
        digits = re.sub(r"[^\d.]", "", s)
        try:
            d = Decimal(digits).quantize(Decimal("0.01"))
        except InvalidOperation:
            raise ValueError(f"not a currency amount: {raw!r}")
        return str(-d if neg else d)
    if typ == "number":
        try:
            return float(re.sub(r"[,$\s]", "", s))
        except ValueError:
            raise ValueError(f"not a number: {raw!r}")
    if typ == "integer":
        if not re.fullmatch(r"-?[\d,]+", s):
            raise ValueError(f"not an integer: {raw!r}")
        return int(s.replace(",", ""))
    if typ == "date":
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                pass
        raise ValueError(f"not a date: {raw!r}")
    if not s:
        raise ValueError("empty value")
    return s
