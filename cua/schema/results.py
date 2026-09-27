"""The replay result contract returned to the calling agent.

Terminal statuses (exactly one):

* ``success``          goal reached, checkpoints verified, outputs returned
* ``business_outcome`` the app gave a legitimate answer the caller must handle
                       (e.g. MEMBER_NOT_FOUND). Not an error; not retryable.
* ``failed``           hard failure with a debuggable ``failure`` block
* ``escalated``        handed to a human and not (yet) resolved

Recoverable conditions (session expired, interstitial notice, transient host
error) are NOT terminal statuses: they are handled inside the run and listed
in ``recoveries`` so the caller and operators can see they happened.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LocatorUse(BaseModel):
    step_id: str
    strategy: str
    rank: int
    degraded: bool = Field(description="True if the top-ranked strategy missed (drift signal).")
    misses: list[str] = Field(default_factory=list)


class Recovery(BaseModel):
    code: str
    at_step: str
    action: str


class BusinessOutcome(BaseModel):
    code: str
    message: str
    at_step: str


class Failure(BaseModel):
    code: Literal[
        "INPUT_INVALID",
        "NOT_APPROVED",
        "POLICY_BLOCKED",
        "APPROVAL_DENIED",
        "LOCATOR_NOT_FOUND",
        "CHECKPOINT_FAILED",
        "OUTPUT_PARSE_ERROR",
        "APP_ERROR",
        "SIGNON_REJECTED",
        "RECOVERY_EXHAUSTED",
        "HUMAN_ABORTED",
        "INTERNAL_ERROR",
    ]
    message: str
    at_step: str | None = None
    step_intent: str | None = None
    expected: Any = None
    observed: Any = None
    url: str | None = None
    screenshot: str | None = None
    dom_snapshot: str | None = None
    retryable: bool = False


class EscalationRecord(BaseModel):
    intervention_id: str
    kind: str
    reason: str
    at_step: str | None
    resolution: str | None
    operator: str | None
    operator_actions: list[dict] = Field(default_factory=list)


class ReplayResult(BaseModel):
    run_id: str
    capability_id: str
    capability_version: str
    capability_hash: str
    status: Literal["success", "business_outcome", "failed", "escalated"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: BusinessOutcome | None = None
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    escalations: list[EscalationRecord] = Field(default_factory=list)
    locators: list[LocatorUse] = Field(default_factory=list)
    human_assisted: bool = False
    duration_ms: int = 0
    evidence_dir: str | None = None
