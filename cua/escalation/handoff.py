"""Pause automation, give the live session to a human, take it back.

``hold_for_operator`` runs on the automation thread (the thread that owns the
browser). It blocks until the operator hands back, aborts, or times out,
executing operator commands that the console queues in the meantime.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

from cua.escalation.control import ControlChannel, Intervention
from cua.evidence import Evidence
from cua.surface.browser import BrowserSurface


def hold_for_operator(
    channel: ControlChannel,
    surface: BrowserSurface,
    evidence: Evidence,
    iv: Intervention,
    timeout_s: float = 900,
) -> tuple[str, str]:
    """Returns (handback_mode, note)."""
    # Capture raw human input in the live page while the operator holds control.
    surface.on_human_event = lambda ev: channel.record_operator_action({"source": "live_page", **ev})
    evidence.event("awaiting_operator", intervention=iv.id, kind=iv.kind, reason=iv.reason,
                   console_hint="open the operator console to claim this session")
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            cmd = channel.next_command(timeout=0.1)
            if cmd is None:
                surface.pump(100)  # keep the page alive; deliver live-page events
                continue
            try:
                result = _execute(cmd.kind, cmd.args, channel, surface, evidence)
            except Exception as e:  # report to the operator, keep holding
                cmd.future.set_result({"ok": False, "error": str(e)})
                continue
            if cmd.kind == "handback":
                mode, note = cmd.args.get("mode", "resume_verify"), cmd.args.get("note", "")
                shot = evidence.screenshot(surface, f"handback-{iv.id}")
                status = "aborted" if mode == "abort" else "resolved"
                channel.close_intervention(status, mode, note)
                evidence.event("handback", intervention=iv.id, mode=mode, note=note,
                               operator=iv.operator, operator_actions=iv.operator_actions, screenshot=shot)
                cmd.future.set_result({"ok": True})
                return mode, note
            cmd.future.set_result(result)
        channel.close_intervention("aborted", "abort", "operator timeout")
        evidence.event("handback", intervention=iv.id, mode="abort", note="operator timeout")
        return "abort", "operator timeout"
    finally:
        surface.on_human_event = lambda ev: None


def _execute(kind: str, args: dict, channel: ControlChannel, surface: BrowserSurface, evidence: Evidence):
    red = surface.redactor
    if kind == "screenshot":
        return surface.screenshot_bytes()
    if kind == "observe":
        obs = surface.observe()
        return {
            "url": red.text(obs.url),
            "landmarks": red.obj(obs.landmarks),
            "controls": [
                red.obj({"control": e.control, "label": e.label, "text": e.text})
                for e in obs.elements if e.control != "text"
            ],
            "page_text": red.text(obs.page_text[:1500]),
        }
    if kind == "handback":
        return {"ok": True}
    if kind == "click_text":
        ok = surface.click_text(args["text"], actor="operator")
        channel.record_operator_action({"source": "console", "action": "click", "text": args["text"], "ok": ok})
        return {"ok": ok}
    if kind == "fill_label":
        ok = surface.fill_by_label(args["label"], args["value"], actor="operator")
        # Operators type override codes/PINs: never record the value itself.
        channel.record_operator_action({"source": "console", "action": "fill", "label": args["label"],
                                        "value": f"[REDACTED len={len(args['value'])}]", "ok": ok})
        return {"ok": ok}
    if kind == "navigate":
        url = urljoin(surface.current_url(), args["path"])
        surface.navigate(url, actor="operator")  # still inside the domain allowlist
        channel.record_operator_action({"source": "console", "action": "navigate", "path": args["path"]})
        return {"ok": True}
    raise ValueError(f"unknown operator command {kind}")
