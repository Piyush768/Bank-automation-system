"""Redaction of regulated data and secrets.

Applied at every *sink*: the event log, DOM snapshots, the text we send to the
LLM, operator-console payloads, and a final check over compiled artifacts.
Screenshots are masked separately (see BrowserSurface.screenshot).

Policy: keep the last 4 digits of SSNs/PANs (standard truncation, enough to
debug "which record"), fully remove credentials and known secret values.
"""

from __future__ import annotations

import re
from typing import Any

SSN_RE = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
PAN_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
BEARER_RE = re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._~+/=-]{8,})")
KV_SECRET_RE = re.compile(r"""(?i)\b(token|api[_-]?key|password|passwd|pwd|secret)(["']?\s*[:=]\s*["']?)([^\s,;"'}]+)""")
PROVIDER_KEY_RE = re.compile(r"\b((?:sk-(?:ant-)?|gsk_)[A-Za-z0-9_\-]{16,})\b")

# Patterns used by the browser to mask screenshots (same definitions).
SCREEN_MASK_PATTERNS = [SSN_RE.pattern, EMAIL_RE.pattern]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


class Redactor:
    def __init__(self, secret_values: list[str] | None = None):
        # Exact secret values (passwords, keys) are removed wherever they appear.
        self._secrets = sorted({s for s in (secret_values or []) if s and len(s) >= 4}, key=len, reverse=True)

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 4 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def text(self, s: str) -> str:
        if not s:
            return s
        for sec in self._secrets:
            s = s.replace(sec, "[REDACTED:SECRET]")
        s = PROVIDER_KEY_RE.sub("[REDACTED:API_KEY]", s)
        s = BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", s)
        s = KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", s)
        s = SSN_RE.sub(lambda m: f"[SSN ***-**-{m.group(3)}]", s)

        def _pan(m: re.Match) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                return f"[PAN ****{digits[-4:]}]"
            return m.group(0)

        s = PAN_CANDIDATE_RE.sub(_pan, s)
        s = EMAIL_RE.sub("[EMAIL]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def is_clean(self, s: str) -> bool:
        return self.text(s) == s
