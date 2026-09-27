"""Stretch goal: agent-facing capability catalog.

Approved artifacts become callable tools. The tool schema is generated from the
artifact contract (inputs/outputs/expected outcomes), so an upstream AI agent
can discover a capability by name and invoke it with typed arguments. The
agent never sees steps, locators, or secrets.
"""

from __future__ import annotations

import glob
import os

from cua.schema.artifact import Capability

_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number"}
_OUT_TYPES = {"currency": ("string", "Decimal amount as a string, 2dp"), "number": ("number", ""),
              "integer": ("integer", ""), "date": ("string", "ISO-8601 date"), "string": ("string", "")}


def load_catalog(directory: str, include_drafts: bool = False) -> dict[str, Capability]:
    caps = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            cap = Capability.load(path)
        except Exception:
            continue
        approved = cap.review.status == "approved" and cap.review.approved_hash == cap.content_hash
        if approved or include_drafts:
            caps[tool_name(cap)] = cap
    return caps


def tool_name(cap: Capability) -> str:
    return cap.id.replace("-", "_")


def tool_definition(cap: Capability) -> dict:
    props = {}
    for p in cap.inputs:
        props[p.name] = {"type": _JSON_TYPES[p.type], "description": p.description}
        if p.pattern:
            props[p.name]["pattern"] = p.pattern
    outcomes = "; ".join(f"{o.code}: {o.description}" for o in cap.expected_outcomes)
    outs = ", ".join(f"{o.name} ({_OUT_TYPES[o.type][0]}{', ' + _OUT_TYPES[o.type][1] if _OUT_TYPES[o.type][1] else ''})"
                     for o in cap.outputs)
    return {
        "name": tool_name(cap),
        "description": (f"{cap.title}. Returns: {outs}. "
                        f"May instead return a business outcome ({outcomes}). "
                        f"Runs deterministically against {cap.app.vendor_app} {cap.app.app_version}; "
                        f"risk={cap.guardrails.max_risk}; version {cap.version} ({cap.content_hash})."),
        "input_schema": {"type": "object", "properties": props, "required": [p.name for p in cap.inputs],
                         "additionalProperties": False},
    }
