"""Wiring: build the surface, policy, evidence, operator channel and engines."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from cua.agent.discovery import DiscoveryAgent
from cua.agent.llm import LLMProvider, make_provider
from cua.compiler import compile_trace, templatize
from cua.escalation.console import ConsoleServer, ScriptedOperator
from cua.escalation.control import ControlChannel
from cua.evidence import Evidence
from cua.replay.engine import ReplayEngine
from cua.safety.policy import Policy
from cua.safety.redact import Redactor
from cua.schema.artifact import Capability
from cua.schema.results import ReplayResult
from cua.surface.browser import BrowserSurface
from cua.tenancy import AppProfile, Tenant, specialise

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "config" / "policy.toml"
PROFILE = ROOT / "config" / "profiles" / "coreline-teller.json"


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def runtime_secrets() -> dict[str, str]:
    """Stand-in for a secret store: values come from the environment only."""
    out = {}
    if os.getenv("BANK_USERNAME"):
        out["username"] = os.environ["BANK_USERNAME"]
    if os.getenv("BANK_PASSWORD"):
        out["password"] = os.environ["BANK_PASSWORD"]
    return out


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"


@contextmanager
def session(evidence_dir: str, run_id: str, operator: str = "none", headed: bool = False,
            console_port: int = 8765, scripted: ScriptedOperator | None = None):
    """Yields (surface, evidence, channel, redactor, policy).

    operator: "none"    -> no human available; stuck == hard failure
              "console" -> start the operator console and wait for a human
              "scripted"-> console + a ScriptedOperator thread (demo/tests)
    """
    secrets = runtime_secrets()
    red = Redactor(list(secrets.values()) + [v for k, v in os.environ.items() if k.endswith("_API_KEY") and v])
    policy = Policy.load(str(POLICY))
    ev = Evidence(evidence_dir, red, run_id)
    surface = BrowserSurface(policy, red, headed=headed,
                             on_blocked=lambda url, why: ev.event("network_blocked", url=url, reason=why))
    channel = console = None
    if operator in ("console", "scripted"):
        channel = ControlChannel(on_event=lambda t, d: ev.event(t, **d), redact=red.obj)
        surface.control_check = channel.assert_can_act
        console = ConsoleServer(channel, console_port).start()
        ev.event("operator_console", url=console.url)
        if operator == "console":
            print(f"[operator console] {console.url}  (open it if the run escalates)")
        if scripted:
            scripted.url = console.url
            scripted.start()
    try:
        yield surface, ev, channel, red, policy
    finally:
        surface.close()
        if console:
            console.stop()
        ev.close()


def discover(*, goal: str, start_url: str, params: dict[str, str], capability_id: str, title: str,
             out_path: str, evidence_dir: str, operator: str = "none", headed: bool = False,
             provider: LLMProvider | None = None, max_steps: int = 25, tenant_id: str | None = None,
             scripted: ScriptedOperator | None = None) -> tuple[Capability | None, dict]:
    llm = provider or make_provider()
    run_id = new_run_id("discovery")
    profile = AppProfile.load(str(PROFILE))
    with session(evidence_dir, run_id, operator, headed, scripted=scripted) as (surface, ev, channel, red, policy):
        agent = DiscoveryAgent(surface, llm, policy, red, ev, channel, max_steps=max_steps)
        trace = agent.run(goal, start_url, params, runtime_secrets())
        summary = {
            "run_id": run_id, "status": trace.status, "summary": trace.summary, "provider": trace.provider,
            "model": trace.model, "llm_calls": trace.llm_calls, "tokens": trace.tokens,
            "steps": len(trace.steps), "outputs": {k: v["sample"] for k, v in trace.outputs.items()},
            "operator_assisted": trace.operator_assisted, "blocked_requests": surface.blocked,
        }
        cap = None
        if trace.status == "done":
            try:
                cap = compile_trace(trace, capability_id=capability_id, title=title,
                                    description=f"Capability discovered from goal: {red.text(templatize(goal, params))}",
                                    profile=profile.raw, policy=policy, redactor=red, tenant_id=tenant_id)
                cap.save(out_path)
                summary["artifact"] = out_path
                summary["content_hash"] = cap.content_hash
                ev.event("artifact_compiled", path=out_path, hash=cap.content_hash, steps=len(cap.steps),
                         outputs=[o.name for o in cap.outputs])
            except Exception as e:
                summary["compile_error"] = str(e)
                ev.event("compile_error", error=str(e))
        ev.write_json("result.json", summary)
    return cap, summary


def replay(*, capability: Capability, params: dict[str, str], tenant_path: str | None, base_url: str | None,
           evidence_dir: str, operator: str = "none", headed: bool = False, allow_draft: bool = False,
           step_timeout_s: float = 8.0, scripted: ScriptedOperator | None = None,
           operator_timeout_s: float = 900) -> ReplayResult:
    profile = AppProfile.load(str(PROFILE))
    run_id = new_run_id("replay")
    with session(evidence_dir, run_id, operator, headed, scripted=scripted) as (surface, ev, channel, red, policy):
        cap = capability
        if tenant_path:
            tenant = Tenant.load(tenant_path)
            cap, changes = specialise(capability, tenant)
            base_url = base_url or tenant.base_url
            ev.event("tenant_overlay", tenant=tenant.tenant_id, changes=changes)
        if not base_url:
            raise ValueError("need --tenant or --base-url")
        engine = ReplayEngine(surface, profile, policy, red, ev, channel, step_timeout_s=step_timeout_s,
                              operator_timeout_s=operator_timeout_s, allow_draft=allow_draft)
        return engine.run(cap, params, runtime_secrets(), base_url, canonical=capability)


def print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))
