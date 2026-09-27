"""Guardrail policy: what the automation may touch and how risky actions are handled.

Enforced in three places so no single bug bypasses it:

1. the agent/replay *decision* path (``check_action`` before acting),
2. the *network* layer (BrowserSurface routes every request through
   ``url_allowed`` and aborts anything off-list, including redirects and
   sub-resources the model never "decided" on),
3. the *artifact* (``Guardrails`` records the max risk; replay re-checks).

Risk classes:
* ``safe``          reads / navigation / typing into a field (no commit)
* ``reversible``    commits that can be undone (save a note, update a field)
* ``irreversible``  moves money, closes/deletes, approves (dual control)
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

Risk = Literal["safe", "reversible", "irreversible"]
_ORDER = {"safe": 0, "reversible": 1, "irreversible": 2}


def risk_at_most(risk: str, ceiling: str) -> bool:
    return _ORDER[risk] <= _ORDER[ceiling]


class Policy(BaseModel):
    allowed_hosts: list[str]
    allowed_path_prefixes: list[str] = ["/"]
    denied_path_prefixes: list[str] = []
    allowed_actions: list[str] = ["navigate", "click", "fill", "select", "press", "extract"]
    irreversible_patterns: list[str] = []
    reversible_patterns: list[str] = []
    irreversible_handling: Literal["block", "require_approval", "flag"] = "require_approval"

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        flat = {**raw.get("allow", {}), **raw.get("risk", {})}
        flat.update({"irreversible_handling": raw.get("handling", {}).get("irreversible", "require_approval")})
        return cls(**flat)

    # ---- URLs ----

    def url_allowed(self, url: str) -> tuple[bool, str]:
        p = urlparse(url)
        if p.scheme in ("data", "about", "blob"):
            return True, "inline"
        if p.scheme not in ("http", "https"):
            return False, f"scheme '{p.scheme}' not allowed"
        if p.hostname not in self.allowed_hosts:
            return False, f"host '{p.hostname}' not in allowlist"
        path = p.path or "/"
        if any(path.startswith(d) for d in self.denied_path_prefixes):
            return False, f"path '{path}' is denied"
        if not any(path.startswith(a) for a in self.allowed_path_prefixes):
            return False, f"path '{path}' not in allowed prefixes"
        return True, "ok"

    # ---- actions ----

    def classify(self, action: str, effect_text: str) -> Risk:
        """Risk of an action from what it would *do* (the control's text/label).

        Typing and selecting never commit anything on their own; the commit
        happens on the click/press that submits, which is classified by the
        submit control's text.
        """
        if action in ("fill", "select", "navigate", "extract"):
            return "safe"
        t = (effect_text or "").lower()
        if any(re.search(p, t) for p in self.irreversible_patterns):
            return "irreversible"
        if any(re.search(p, t) for p in self.reversible_patterns):
            return "reversible"
        return "safe"

    def check_action(self, action: str, risk: Risk) -> "Decision":
        if action not in self.allowed_actions:
            return Decision("block", f"action '{action}' not in allowlist")
        if risk == "irreversible":
            h = self.irreversible_handling
            if h == "block":
                return Decision("block", "irreversible actions are blocked by policy")
            if h == "require_approval":
                return Decision("approve", "irreversible action requires human approval")
            return Decision("allow", "irreversible action flagged")
        return Decision("allow", "ok")


@dataclass
class Decision:
    verdict: Literal["allow", "block", "approve"]
    reason: str
