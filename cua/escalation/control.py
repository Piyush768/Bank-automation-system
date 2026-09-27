"""The control-transfer model for human-in-the-loop escalation.

One live session, one owner at a time::

    AUTOMATION --request()--> AWAITING_OPERATOR --claim(op)--> OPERATOR
        ^                                                          |
        +------------------ handback(mode, note) ------------------+

* The surface asks ``assert_can_act(actor)`` before *every* action, so
  automation physically cannot click while a human holds the session (and the
  console cannot act unless an operator has claimed it).
* Operator commands arrive from the console's HTTP thread but are executed on
  the automation thread via a queue, because the browser session is owned by
  one thread. The console never touches the browser directly.
* Everything the operator does is recorded on the Intervention: structured
  console commands, plus raw clicks/inputs captured in the live page when the
  operator uses the real browser window (--headed).
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal

from cua.surface.browser import ControlViolation


class Owner(str, Enum):
    AUTOMATION = "automation"
    AWAITING_OPERATOR = "awaiting_operator"
    OPERATOR = "operator"


InterventionKind = Literal["stuck", "approval", "unknown_state", "unrecoverable"]
HandbackMode = Literal["resume_verify", "retry_step", "step_done", "approve", "deny", "abort"]


@dataclass
class Intervention:
    id: str
    kind: InterventionKind
    reason: str
    context: dict[str, Any]
    created_at: str
    status: Literal["open", "claimed", "resolved", "aborted"] = "open"
    operator: str | None = None
    operator_actions: list[dict] = field(default_factory=list)
    resolution: str | None = None
    note: str = ""
    screenshot: str | None = None

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Command:
    kind: str  # observe | screenshot | click_text | fill_label | press | navigate | handback
    args: dict
    future: Future


class ControlChannel:
    def __init__(self, on_event: Callable[[str, dict], None] | None = None, redact: Callable[[Any], Any] | None = None):
        self._redact = redact or (lambda o: o)  # everything shown to operators is redacted
        self._lock = threading.Lock()
        self.owner = Owner.AUTOMATION
        self.intervention: Intervention | None = None
        self.history: list[Intervention] = []
        self._q: "queue.Queue[Command]" = queue.Queue()
        self._on_event = on_event or (lambda t, d: None)

    # ---- enforced by the surface before every action ----
    def assert_can_act(self, actor: str) -> None:
        with self._lock:
            if actor == "automation" and self.owner != Owner.AUTOMATION:
                raise ControlViolation(f"automation cannot act: session owned by {self.owner.value}")
            if actor == "operator" and self.owner != Owner.OPERATOR:
                raise ControlViolation("operator must claim the session first")

    # ---- automation side ----
    def request(self, kind: InterventionKind, reason: str, context: dict, screenshot: str | None) -> Intervention:
        with self._lock:
            iv = Intervention(
                id="iv-" + uuid.uuid4().hex[:8],
                kind=kind,
                reason=self._redact(reason),
                context=self._redact(context),
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                screenshot=screenshot,
            )
            self.intervention = iv
            self.owner = Owner.AWAITING_OPERATOR
        self._on_event("intervention_requested", {"id": iv.id, "kind": kind, "reason": reason, "context": context})
        return iv

    def next_command(self, timeout: float) -> Command | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close_intervention(self, status: str, resolution: str, note: str) -> None:
        with self._lock:
            iv = self.intervention
            if iv:
                iv.status = status
                iv.resolution = resolution
                iv.note = note
                self.history.append(iv)
            self.intervention = None
            self.owner = Owner.AUTOMATION
        self._on_event("control_returned", {"id": iv.id if iv else None, "resolution": resolution, "note": note})

    # ---- operator side (called from the console thread) ----
    def claim(self, operator: str) -> Intervention:
        with self._lock:
            iv = self.intervention
            if not iv or self.owner != Owner.AWAITING_OPERATOR:
                raise ControlViolation(f"nothing to claim (owner={self.owner.value})")
            iv.status = "claimed"
            iv.operator = operator
            self.owner = Owner.OPERATOR
        self._on_event("control_claimed", {"id": iv.id, "operator": operator})
        return iv

    def submit(self, kind: str, args: dict, timeout: float = 30) -> Any:
        if kind != "screenshot" and kind != "observe":
            with self._lock:
                if self.owner != Owner.OPERATOR:
                    raise ControlViolation("claim the session before sending commands")
        fut: Future = Future()
        self._q.put(Command(kind, args, fut))
        return fut.result(timeout=timeout)

    def record_operator_action(self, action: dict) -> None:
        with self._lock:
            if self.intervention and self.owner == Owner.OPERATOR:
                action = {"t": time.strftime("%H:%M:%S"), **action}
                self.intervention.operator_actions.append(action)
                self._on_event("operator_action", {"id": self.intervention.id, **action})

    def state(self) -> dict:
        with self._lock:
            return {
                "owner": self.owner.value,
                "intervention": self.intervention.public() if self.intervention else None,
                "history": [i.id for i in self.history],
            }
