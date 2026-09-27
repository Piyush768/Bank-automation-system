"""Goal-driven discovery: observe -> decide (LLM) -> act, recorded as a trace.

The trace is the raw material the compiler turns into a capability artifact.
Discovery is the ONLY place a model makes decisions.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cua.agent.llm import LLMProvider
from cua.agent.prompts import SYSTEM, build_user_prompt
from cua.escalation.control import ControlChannel
from cua.escalation.handoff import hold_for_operator
from cua.evidence import Evidence
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.schema.artifact import Target
from cua.surface.base import Observation, UIElement
from cua.surface.browser import BrowserSurface

SECRET_RE = re.compile(r"\{\{secrets\.([a-z_][a-z0-9_]*)\}\}")


@dataclass
class TraceStep:
    n: int
    action: str
    thought: str
    origin: str = "model"
    target: Target | None = None
    effect_text: str = ""
    value_template: str | None = None
    risk: str = "safe"
    url_before: str = ""
    url_after: str = ""
    landmarks_before: list[str] = field(default_factory=list)
    landmarks_after: list[str] = field(default_factory=list)
    expect_text: str | None = None
    expect_verified: bool = False
    ok: bool = True
    error: str | None = None
    output_name: str | None = None
    output_type: str | None = None
    screenshot: str | None = None


@dataclass
class DiscoveryTrace:
    run_id: str
    goal: str
    start_url: str
    params: dict[str, str]
    secret_names: list[str]
    provider: str
    model: str
    steps: list[TraceStep] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> {type, target, sample}
    status: str = "running"  # done | failed | aborted
    summary: str = ""
    operator_assisted: bool = False
    operator_actions: list[dict] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    llm_calls: int = 0


class DiscoveryAgent:
    def __init__(
        self,
        surface: BrowserSurface,
        llm: LLMProvider,
        policy: Policy,
        redactor: Redactor,
        evidence: Evidence,
        channel: ControlChannel | None = None,
        max_steps: int = 25,
        operator_timeout_s: float = 900,
    ):
        self.s, self.llm, self.policy, self.red, self.ev = surface, llm, policy, redactor, evidence
        self.channel = channel
        self.max_steps = max_steps
        self.operator_timeout_s = operator_timeout_s

    # ------------------------------------------------------------------ main

    def run(self, goal: str, start_url: str, params: dict[str, str], secrets: dict[str, str]) -> DiscoveryTrace:
        tr = DiscoveryTrace(
            run_id=self.ev.run_id, goal=goal, start_url=start_url, params=params,
            secret_names=sorted(secrets), provider=self.llm.name, model=self.llm.model,
        )
        self.ev.event("discovery_start", goal=goal, start_url=start_url, params=params,
                      secrets=[f"{{{{secrets.{n}}}}}" for n in secrets], provider=self.llm.name, model=self.llm.model,
                      max_steps=self.max_steps)
        self.s.navigate(start_url)
        history: list[str] = []
        note: str | None = None
        recent: list[str] = []
        consecutive_errors = 0

        for n in range(1, self.max_steps + 1):
            obs = self.s.observe()
            model_obs = self._redact_obs(obs)
            prompt = build_user_prompt(goal, params, list(secrets), model_obs, history,
                                       {k: v["sample"] for k, v in tr.outputs.items()}, note)
            note = None
            t0 = time.time()
            try:
                d = self.llm.decide(SYSTEM, prompt)
            except Exception as e:
                self.ev.event("llm_error", step=n, error=str(e))
                tr.status, tr.summary = "failed", f"LLM call failed: {e}"
                break
            tr.llm_calls += 1
            tr.tokens["input"] += d.usage.get("input_tokens") or 0
            tr.tokens["output"] += d.usage.get("output_tokens") or 0
            self.ev.event("decision", step=n, url=obs.url, landmarks=obs.landmarks, decision=d.raw,
                          latency_ms=int((time.time() - t0) * 1000), usage=d.usage)

            action = d.action
            el = obs.element(d.element) if d.element is not None else None

            # ---- terminal decisions ----
            if action == "done":
                if not tr.outputs:
                    note = "You called done but extracted nothing. Extract the requested value(s) first."
                    history.append(f"{n}. done REJECTED: nothing extracted yet")
                    continue
                tr.status, tr.summary = "done", d.summary or ""
                tr.steps.append(TraceStep(n=n, action="done", thought=d.thought or "",
                                          landmarks_after=obs.landmarks, url_after=obs.url))
                break
            if action == "escalate":
                if not self._escalate(tr, "stuck", d.summary or "model asked for help", obs, n, history):
                    break
                note = "A human operator intervened. Re-read the screen and continue toward the goal."
                continue

            # ---- validate the decision ----
            if action not in ("click", "fill", "select", "press", "extract") or el is None:
                history.append(f"{n}. INVALID decision {action} element={d.element}: choose a listed element id")
                consecutive_errors += 1
                if consecutive_errors >= 3 and not self._escalate(tr, "stuck", "repeated invalid decisions", obs, n, history):
                    break
                continue

            # ---- extraction (read-only) ----
            if action == "extract":
                raw = self.s.read_index(el.idx)
                target = self.s.describe_target(el.idx, obs, params)
                name = _snake(d.output_name or f"value_{n}")
                tr.outputs[name] = {"type": d.output_type or "string", "target": target, "sample": raw}
                tr.steps.append(TraceStep(n=n, action="extract", thought=d.thought or "", target=target,
                                          output_name=name, output_type=d.output_type, url_after=obs.url,
                                          landmarks_after=obs.landmarks))
                self.ev.event("extracted", step=n, name=name, type=d.output_type, value=raw,
                              target=target.model_dump(), locator_count=len(target.strategies))
                history.append(f"{n}. extract {name} from '{target.description}' -> '{self.red.text(raw)}'")
                consecutive_errors = 0
                continue

            # ---- stuck detection: same screen, same action, three times ----
            sig = f"{obs.url}|{'/'.join(obs.landmarks)}|{action}|{el.label or el.text}|{d.value}"
            recent.append(sig)
            if recent[-3:].count(sig) == 3:
                if not self._escalate(tr, "stuck", "repeating the same action on the same screen", obs, n, history):
                    break
                recent.clear()
                note = "A human operator intervened after you repeated an action. Re-read the screen."
                continue

            # ---- guardrails before acting ----
            risk = self.policy.classify(action, el.effect_text or el.text or el.name)
            verdict = self.policy.check_action(action, risk)
            self.ev.event("policy_check", step=n, action=action, risk=risk, verdict=verdict.verdict,
                          reason=verdict.reason, control=el.effect_text or el.text)
            if verdict.verdict == "block":
                history.append(f"{n}. BLOCKED by policy: {verdict.reason}. Choose another way or escalate.")
                continue
            if verdict.verdict == "approve":
                approved = self._approval(tr, obs, n, el, action, risk, history)
                if approved is None:
                    break
                if not approved:
                    history.append(f"{n}. {action} on '{el.text or el.label}' DENIED by human approver")
                    note = "The human approver denied the risky action. Do not retry it; finish or escalate."
                    continue

            # ---- record the target BEFORE acting (the element may vanish after) ----
            target = self.s.describe_target(el.idx, obs, params)
            value_template = d.value
            actual_value = self._inject_secrets(d.value, secrets) if action in ("fill", "select", "press") else None
            step = TraceStep(
                n=n, action=action, thought=d.thought or "", target=target, effect_text=el.effect_text,
                value_template=value_template, risk=risk, url_before=obs.url, landmarks_before=obs.landmarks,
                expect_text=d.expect_text,
            )
            try:
                self.s.act_on_index(el.idx, action, actual_value)
                consecutive_errors = 0
            except Exception as e:
                step.ok, step.error = False, str(e).splitlines()[0][:200]
                consecutive_errors += 1
            step.url_after = self.s.current_url()
            step.landmarks_after = self.s.landmarks()
            if step.ok and d.expect_text:
                step.expect_verified = _norm(d.expect_text) in _norm(self.s.page_text())
            step.screenshot = self.ev.screenshot(self.s, f"step{n}-{action}")
            tr.steps.append(step)
            self.ev.event("action", step=n, action=action, target=target.description, strategies=[x.kind for x in target.strategies],
                          value=value_template, risk=risk, ok=step.ok, error=step.error, url_after=step.url_after,
                          new_landmarks=[x for x in step.landmarks_after if x not in step.landmarks_before],
                          expect_text=d.expect_text, expect_verified=step.expect_verified, screenshot=step.screenshot)
            outcome = "ok" if step.ok else f"ERROR {step.error}"
            shown = value_template if value_template else ""
            history.append(f"{n}. {action} '{target.description}'{(' = ' + repr(shown)) if shown else ''} -> {outcome}; "
                           f"screen now: {', '.join(step.landmarks_after[-2:])}")
            if consecutive_errors >= 3:
                if not self._escalate(tr, "stuck", "three consecutive action errors", obs, n, history):
                    break
                consecutive_errors = 0
        else:
            tr.status, tr.summary = "failed", f"max_steps ({self.max_steps}) reached"
            if self.channel:
                self._escalate(tr, "stuck", "step budget exhausted", self.s.observe(), self.max_steps, history)

        if tr.status == "running":
            tr.status = "failed"
        tr.operator_actions = [a for iv in (self.channel.history if self.channel else []) for a in iv.operator_actions]
        self.ev.event("discovery_end", status=tr.status, summary=tr.summary, steps=len(tr.steps),
                      outputs={k: v["sample"] for k, v in tr.outputs.items()}, llm_calls=tr.llm_calls,
                      tokens=tr.tokens, operator_assisted=tr.operator_assisted)
        return tr

    # ------------------------------------------------------------ helpers

    def _redact_obs(self, obs: Observation) -> Observation:
        """Data minimisation: the model never needs SSNs/PANs to operate a screen."""
        els = [UIElement(**self.red.obj(e.model_dump())) for e in obs.elements]
        return obs.model_copy(update={
            "elements": els, "page_text": self.red.text(obs.page_text),
            "landmarks": self.red.obj(obs.landmarks),
        })

    def _inject_secrets(self, value: str | None, secrets: dict[str, str]) -> str | None:
        if value is None:
            return None

        def sub(m: re.Match) -> str:
            if m.group(1) not in secrets:
                raise KeyError(f"unknown secret {m.group(1)}")
            return secrets[m.group(1)]

        return SECRET_RE.sub(sub, value)

    def _escalate(self, tr: DiscoveryTrace, kind: str, reason: str, obs: Observation, n: int, history: list[str]) -> bool:
        """Returns True if the run should continue after the human hands back."""
        shot = self.ev.screenshot(self.s, f"escalation-step{n}")
        self.ev.dom(self.s, f"escalation-step{n}")
        if not self.channel:
            self.ev.event("escalation_unavailable", step=n, kind=kind, reason=reason, screenshot=shot)
            tr.status, tr.summary = "failed", f"stuck and no operator available: {reason}"
            return False
        iv = self.channel.request(kind, reason, {"goal": tr.goal, "step": n, "url": obs.url,
                                                 "landmarks": obs.landmarks, "recent": history[-3:]}, shot)
        mode, note = hold_for_operator(self.channel, self.s, self.ev, iv, self.operator_timeout_s)
        tr.operator_assisted = True
        history.append(f"{n}. HUMAN OPERATOR took control ({reason}); handed back with '{mode}': {note}")
        if mode == "abort":
            tr.status, tr.summary = "aborted", f"operator aborted: {note}"
            return False
        return True

    def _approval(self, tr, obs, n, el, action, risk, history) -> bool | None:
        """Dual control for irreversible actions. True=approved, False=denied, None=abort."""
        shot = self.ev.screenshot(self.s, f"approval-step{n}")
        if not self.channel:
            self.ev.event("approval_unavailable", step=n)
            return False
        iv = self.channel.request("approval", f"{risk} action: {action} '{el.effect_text or el.text}'",
                                  {"goal": tr.goal, "step": n, "url": obs.url, "control": el.effect_text or el.text}, shot)
        mode, note = hold_for_operator(self.channel, self.s, self.ev, iv, self.operator_timeout_s)
        tr.operator_assisted = True
        if mode == "abort":
            tr.status, tr.summary = "aborted", f"operator aborted at approval: {note}"
            return None
        return mode == "approve"


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _snake(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "value"
