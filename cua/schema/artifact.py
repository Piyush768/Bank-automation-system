"""The capability artifact: the contract between discovery, review, and replay.

Design goals (see REPORT.md section 2):

* **Agent-invocable**: typed ``inputs`` / ``outputs`` / ``expected_outcomes``
  read like a function signature, so it maps 1:1 onto a tool definition.
* **Reviewable**: every step carries its ``intent`` (why), a human
  ``description`` of its target, its ``risk`` class, and what we ``expect`` to
  see afterwards. A reviewer can approve it without reading a transcript.
* **Surface-neutral where possible**: locators are expressed in terms a human
  operator would use (the label next to a field, a table row + column, visible
  text), which also exist on legacy web, desktop (UIA names) and terminal
  screens. Surface-specific locators (CSS) are kept but ranked last.
* **Tenant-portable**: no base URL is baked in (only paths), and every
  user-visible string is overridable by a tenant overlay.
* **Never holds data**: parameter values are templated out
  (``{{params.member_id}}``), credentials are references
  (``{{secrets.password}}``). The compiler refuses to emit an artifact that
  still contains an example value or matches a PII pattern.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_ID = "cua.capability/v1"

Control = Literal["input", "select", "clickable", "text"]
Risk = Literal["safe", "reversible", "irreversible"]


# --------------------------------------------------------------------------
# Locator strategies. A Target holds several, ranked most-stable first.
# Every strategy must resolve to exactly ONE element of the expected control
# type, otherwise it is treated as a miss (never "pick the first match").
# --------------------------------------------------------------------------


class LabelLocator(BaseModel):
    """The control sitting next to a visible label, e.g. input right of 'Member Number:'.

    Works without <label> tags, IDs or ARIA: the same idea as a human reading
    the screen. Maps directly to desktop (UIA 'LabeledBy'/adjacent static text)
    and terminal screens (field following a prompt).
    """

    kind: Literal["label"] = "label"
    label: str
    relation: Literal["right_of", "same_row"] = "right_of"
    control: Control


class TableCellLocator(BaseModel):
    """A cell addressed by row *content* and column *header*, never by position.

    Row order differs between records (see member 20318), so "row 1 col 3" is
    wrong; "the row containing PRIMARY SAVINGS, column BALANCE" is not.
    """

    kind: Literal["table_cell"] = "table_cell"
    row_contains: str
    column: str
    control: Control = "text"


class TextLocator(BaseModel):
    """A control whose own visible text is exactly this (buttons, links, spans)."""

    kind: Literal["text"] = "text"
    text: str
    control: Control = "clickable"


class RoleLocator(BaseModel):
    """Accessibility role + accessible name. Strong when present; legacy apps often lack it."""

    kind: Literal["role"] = "role"
    role: str
    name: str


class CssLocator(BaseModel):
    """Structural DOM path. Web-only and layout-sensitive: kept as a late fallback."""

    kind: Literal["css"] = "css"
    selector: str
    control: Control


class CoordsLocator(BaseModel):
    """Viewport coordinates. Last resort for pixel-only surfaces (Citrix, canvas).

    Using it on replay is always reported as degraded.
    """

    kind: Literal["coords"] = "coords"
    x: float
    y: float
    viewport_w: int
    viewport_h: int
    control: Control


Locator = Annotated[
    Union[LabelLocator, TableCellLocator, TextLocator, RoleLocator, CssLocator, CoordsLocator],
    Field(discriminator="kind"),
]


class Target(BaseModel):
    description: str = Field(description="Human-readable: what a reviewer should picture.")
    control: Control
    strategies: list[Locator] = Field(min_length=1, description="Ranked, most stable first.")


class Checkpoint(BaseModel):
    """A condition asserted after a step: proof we reached the expected state."""

    kind: Literal["text_present", "url_path"]
    value: str
    source: Literal["model_verified", "observed_landmark", "observed_url"] = "observed_landmark"


# --------------------------------------------------------------------------
# Steps, contract, and metadata
# --------------------------------------------------------------------------


class Step(BaseModel):
    id: str
    action: Literal["navigate", "click", "fill", "select", "press"]
    intent: str = Field(description="Why this step exists (from discovery reasoning, redacted).")
    target: Target | None = None
    value: str | None = Field(None, description="Template: literal, {{params.x}} or {{secrets.y}}.")
    url_path: str | None = Field(None, description="For navigate: path only; base URL comes from tenant.")
    risk: Risk = "safe"
    expect: list[Checkpoint] = Field(default_factory=list)
    origin: Literal["model", "operator"] = "model"


class InputParam(BaseModel):
    name: str
    type: Literal["string", "integer", "number"] = "string"
    pattern: str | None = None
    description: str = ""
    inferred: bool = Field(True, description="Pattern inferred from one example; confirm in review.")


class SecretRef(BaseModel):
    name: str
    description: str = "Injected at runtime from the secret store; never persisted."


class OutputField(BaseModel):
    name: str
    type: Literal["string", "number", "currency", "integer", "date"]
    description: str = ""
    source: Target


class ExpectedOutcome(BaseModel):
    """A legitimate business result the caller must handle (not an error)."""

    code: str
    description: str


class AppRef(BaseModel):
    vendor_app: str
    app_version: str
    profile: str = Field(description="Condition catalog for this vendor app, e.g. coreline-teller@1")
    surface: Literal["web", "desktop", "terminal"] = "web"


class Guardrails(BaseModel):
    allowed_actions: list[str]
    max_risk: Risk
    irreversible_requires_approval: bool = True


class Provenance(BaseModel):
    discovery_run_id: str
    discovered_at: datetime
    provider: str
    model: str
    goal: str
    compiler_version: str
    operator_assisted: bool = False
    discovery_tenant: str | None = None


class Review(BaseModel):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    approved_by: str | None = None
    approved_at: datetime | None = None
    approved_hash: str | None = Field(None, description="content_hash that was approved; edits void approval.")
    notes: str = ""


class Capability(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_id: str = Field(SCHEMA_ID, alias="schema")
    id: str
    version: str = "1.0.0"
    title: str
    description: str
    app: AppRef
    entry_path: str
    inputs: list[InputParam] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)
    steps: list[Step]
    success: list[Checkpoint] = Field(min_length=1)
    guardrails: Guardrails
    provenance: Provenance
    review: Review = Field(default_factory=Review)
    content_hash: str = ""

    # ---- helpers ----

    def compute_hash(self) -> str:
        """Hash of the behaviour-defining content (excludes review + hash itself).

        Approval is bound to this hash: editing a step invalidates approval.
        """
        data = self.model_dump(mode="json", by_alias=True, exclude={"review", "content_hash"})
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2, exclude_none=True)

    @classmethod
    def load(cls, path: str) -> "Capability":
        with open(path) as f:
            return cls.model_validate_json(f.read())

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            f.write(self.to_json() + "\n")
