"""Vendor-app profiles and tenant overlays.

* A **profile** belongs to a vendor product + version (coreline-teller@1). It
  holds the condition catalog: error screens, notices, timeouts. Every tenant
  running that product, and every capability recorded on it, shares it.
* A **tenant binding** says where an institution's instance lives (base URL)
  and how its configuration differs: here, a text-override table for renamed
  labels, buttons, menu items and column headers.

A capability is recorded once (canonical), then *specialised* per tenant at
load time by applying the overlay to locator texts and checkpoints. Nothing is
re-recorded; the specialised copy records which overlay produced it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from cua.schema.artifact import Capability


@dataclass
class Condition:
    code: str
    cls: str  # business | recoverable | fatal
    pattern: re.Pattern
    message: str
    recovery: dict = field(default_factory=dict)


@dataclass
class AppProfile:
    profile: str
    vendor_app: str
    app_version: str
    conditions: list[Condition]
    raw: dict

    @classmethod
    def load(cls, path: str) -> "AppProfile":
        with open(path) as f:
            raw = json.load(f)
        conds = [
            Condition(c["code"], c["class"], re.compile(c["match"], re.I), c["message"], c.get("recovery", {}))
            for c in raw["conditions"]
        ]
        return cls(raw["profile"], raw["vendor_app"], raw["app_version"], conds, raw)

    def detect(self, page_text: str) -> Condition | None:
        # Business outcomes and fatal conditions win over recoverable noise.
        hits = [c for c in self.conditions if c.pattern.search(page_text or "")]
        order = {"fatal": 0, "business": 1, "recoverable": 2}
        return sorted(hits, key=lambda c: order[c.cls])[0] if hits else None


@dataclass
class Tenant:
    tenant_id: str
    base_url: str
    vendor_app: str
    app_version: str
    text_overrides: dict[str, str]

    @classmethod
    def load(cls, path: str) -> "Tenant":
        with open(path) as f:
            raw = json.load(f)
        return cls(raw["tenant_id"], raw["base_url"], raw["vendor_app"], raw["app_version"], raw.get("text_overrides", {}))


# Only these fields are "chrome" text that a tenant can rename.
_OVERRIDABLE = {"label", "text", "name", "column", "row_contains", "value"}


def specialise(cap: Capability, tenant: Tenant) -> tuple[Capability, list[str]]:
    """Apply a tenant's text overrides to locators and checkpoints.

    Returns the specialised capability and a list of what was changed, which
    is logged so drift/overrides are auditable. Refuses a vendor/version
    mismatch instead of guessing.
    """
    if (tenant.vendor_app, tenant.app_version) != (cap.app.vendor_app, cap.app.app_version):
        raise ValueError(
            f"tenant {tenant.tenant_id} runs {tenant.vendor_app} {tenant.app_version}; "
            f"capability targets {cap.app.vendor_app} {cap.app.app_version}"
        )
    if not tenant.text_overrides:
        return cap, []
    changes: list[str] = []
    data = cap.model_dump(mode="json", by_alias=True)

    def walk(o, key=None):
        if isinstance(o, dict):
            return {k: walk(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v, key) for v in o]
        if isinstance(o, str) and key in _OVERRIDABLE and o in tenant.text_overrides:
            changes.append(f"{o!r} -> {tenant.text_overrides[o]!r}")
            return tenant.text_overrides[o]
        return o

    for st in data["steps"]:
        if st.get("target"):
            st["target"] = walk(st["target"])
        st["expect"] = walk(st["expect"])
        for cp in st["expect"]:
            if cp["value"] in tenant.text_overrides:
                changes.append(f"{cp['value']!r} -> {tenant.text_overrides[cp['value']]!r}")
                cp["value"] = tenant.text_overrides[cp["value"]]
    for o in data["outputs"]:
        o["source"] = walk(o["source"])
    for cp in data["success"]:
        if cp["value"] in tenant.text_overrides:
            changes.append(f"{cp['value']!r} -> {tenant.text_overrides[cp['value']]!r}")
            cp["value"] = tenant.text_overrides[cp["value"]]
    return Capability.model_validate(data), changes
