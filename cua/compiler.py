"""Compile a discovery trace into a capability artifact.

Decoupled from the model transcript: we keep *what worked* (actions, validated
locators, grounded checkpoints, typed I/O) and drop *how the model reasoned*
except for a one-line intent per step for reviewers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from cua.agent.discovery import DiscoveryTrace, TraceStep
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.schema.artifact import (
    AppRef,
    Capability,
    Checkpoint,
    ExpectedOutcome,
    Guardrails,
    InputParam,
    OutputField,
    Provenance,
    SecretRef,
    Step,
)

COMPILER_VERSION = "cua-compiler/0.1"


class CompileError(ValueError):
    pass


def templatize(s: str | None, params: dict[str, str]) -> str | None:
    """Replace example parameter values with {{params.name}} (longest first)."""
    if s is None:
        return None
    for name, val in sorted(params.items(), key=lambda kv: -len(kv[1] or "")):
        if val:
            s = s.replace(val, "{{params." + name + "}}")
    return s


def _templatize_model(obj, params):
    """Deep-templatize every string field of a pydantic model / dict / list."""
    if isinstance(obj, str):
        return templatize(obj, params)
    if isinstance(obj, dict):
        return {k: _templatize_model(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_templatize_model(v, params) for v in obj]
    return obj


def _intent(action: str, description: str, value: str | None) -> str:
    """Deterministic, data-free step intent. The model's free-text reasoning stays
    in the discovery log: it can mention record data (names, balances) and must
    not be copied into a reusable artifact."""
    verb = {"click": "Click", "fill": "Type", "select": "Choose", "press": "Press"}.get(action, action)
    if action in ("fill", "select") and value:
        return f"{verb} {value} into the {description}"
    if action == "press":
        return f"{verb} {value or 'Enter'} in the {description}"
    return f"{verb} {description}"


def _infer_param(name: str, example: str) -> InputParam:
    if re.fullmatch(r"\d+", example):
        return InputParam(name=name, type="string", pattern=rf"^\d{{{len(example)}}}$",
                          description=f"{len(example)}-digit identifier (pattern inferred from one example)")
    if re.fullmatch(r"-?\d+(\.\d+)?", example):
        return InputParam(name=name, type="number", description="numeric (inferred)")
    return InputParam(name=name, type="string", description="free text (inferred)")


def _checkpoints(ts: TraceStep, params: dict[str, str]) -> list[Checkpoint]:
    """Grounded checkpoints: only text we actually saw appear after the step."""
    cps: list[Checkpoint] = []
    if ts.expect_text and ts.expect_verified:
        cps.append(Checkpoint(kind="text_present", value=templatize(ts.expect_text, params), source="model_verified"))
    new = [x for x in ts.landmarks_after if x not in ts.landmarks_before]
    # Prefer screen titles over data: skip landmarks that are mostly digits or record values.
    for lm in new:
        if re.search(r"[A-Za-z]{4}", lm) and not re.search(r"\d{3,}", lm):
            if not any(c.value.lower() == lm.lower() for c in cps):
                cps.append(Checkpoint(kind="text_present", value=lm, source="observed_landmark"))
            break
    before, after = urlparse(ts.url_before).path, urlparse(ts.url_after).path
    if after and after != before and not cps:
        cps.append(Checkpoint(kind="url_path", value=templatize(after, params), source="observed_url"))
    return cps


def compile_trace(
    tr: DiscoveryTrace,
    *,
    capability_id: str,
    title: str,
    description: str,
    profile: dict,
    policy: Policy,
    redactor: Redactor,
    tenant_id: str | None = None,
) -> Capability:
    if tr.status != "done":
        raise CompileError(f"discovery did not complete (status={tr.status}): {tr.summary}")
    params = tr.params
    for k, v in params.items():
        if len(v) < 3:
            raise CompileError(f"example for param '{k}' is too short to templatize safely: use a distinctive value")

    steps: list[Step] = [Step(
        id="s00-open-entry", action="navigate", intent="Open the application entry screen",
        url_path=urlparse(tr.start_url).path or "/", risk="safe",
    )]
    secrets_used: set[str] = set()
    acted = [s for s in tr.steps if s.ok and s.action in ("click", "fill", "select", "press")]
    for i, ts in enumerate(acted, start=1):
        value = ts.value_template
        if value:
            secrets_used.update(re.findall(r"\{\{secrets\.([a-z_][a-z0-9_]*)\}\}", value))
            value = templatize(value, params)
        target = ts.target.model_copy(deep=True)
        target = target.model_validate(_templatize_model(target.model_dump(), params))
        slug = re.sub(r"[^a-z0-9]+", "-", f"{ts.action} {target.description}".lower()).strip("-")[:40]
        steps.append(Step(
            id=f"s{i:02d}-{slug}", action=ts.action, intent=_intent(ts.action, target.description, value), target=target,
            value=value, risk=ts.risk, expect=_checkpoints(ts, params), origin=ts.origin,
        ))

    outputs = []
    for name, o in tr.outputs.items():
        tgt = o["target"].model_validate(_templatize_model(o["target"].model_dump(), params))
        outputs.append(OutputField(name=name, type=o["type"], source=tgt,
                                   description=f"Read from {tgt.description}"))

    # Success = the screen title the outputs were read on (a landmark we actually saw),
    # excluding branding that is on every screen and record data.
    final_lms = next((s.landmarks_after for s in reversed(tr.steps) if s.landmarks_after), [])
    branding = set(tr.steps[0].landmarks_before) if tr.steps and tr.steps[0].landmarks_before else set()
    titles = [lm for lm in final_lms
              if lm not in branding and re.search(r"[A-Za-z]{4}", lm) and not re.search(r"\d{3,}", lm)]
    if titles:
        success = [Checkpoint(kind="text_present", value=titles[0], source="observed_landmark")]
    elif steps[-1].expect:
        success = [steps[-1].expect[-1]]
    else:
        raise CompileError("could not derive a success checkpoint from the final screen")

    business = [ExpectedOutcome(code=c["code"], description=c["message"])
                for c in profile["conditions"] if c["class"] == "business"]
    max_risk = max((s.risk for s in steps), key=["safe", "reversible", "irreversible"].index)

    cap = Capability(
        id=capability_id,
        title=title,
        description=description,
        app=AppRef(vendor_app=profile["vendor_app"], app_version=profile["app_version"], profile=profile["profile"]),
        entry_path=urlparse(tr.start_url).path or "/",
        inputs=[_infer_param(k, v) for k, v in params.items()],
        secrets=[SecretRef(name=n) for n in sorted(secrets_used)],
        outputs=outputs,
        expected_outcomes=business,
        steps=steps,
        success=success,
        guardrails=Guardrails(
            allowed_actions=sorted({s.action for s in steps} | ({"extract"} if outputs else set())),
            max_risk=max_risk,
            irreversible_requires_approval=policy.irreversible_handling != "flag",
        ),
        provenance=Provenance(
            discovery_run_id=tr.run_id, discovered_at=datetime.now(timezone.utc), provider=tr.provider,
            model=tr.model, goal=redactor.text(templatize(tr.goal, params)), compiler_version=COMPILER_VERSION,
            operator_assisted=tr.operator_assisted, discovery_tenant=tenant_id,
        ),
    )
    if tr.operator_assisted:
        cap.review.notes = ("Operator intervened during discovery; their actions are in the discovery log and were "
                            "NOT compiled into steps. Review before approval.")
    cap.content_hash = cap.compute_hash()
    _assert_no_data(cap, tr, redactor)
    return cap


def _assert_no_data(cap: Capability, tr: DiscoveryTrace, redactor: Redactor) -> None:
    """Last line of defence: an artifact must never carry example inputs,
    extracted values, secrets, or PII patterns."""
    blob = cap.to_json()
    if not redactor.is_clean(blob):
        raise CompileError("artifact contains data matching a PII/secret pattern; refusing to emit")
    body = json.loads(blob)
    body.pop("provenance", None)
    flat = json.dumps(body)
    for k, v in tr.params.items():
        if v and v in flat:
            raise CompileError(f"example value of param '{k}' leaked into the artifact")
    for name, o in tr.outputs.items():
        sample = (o.get("sample") or "").strip()
        if len(sample) >= 4 and sample in flat:
            raise CompileError(f"extracted sample of '{name}' leaked into the artifact")
