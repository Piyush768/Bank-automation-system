"""Run evidence: a structured, redacted event log plus screenshots and DOM snapshots.

Layout of one run directory::

    events.jsonl      one JSON object per event (seq, ts, type, ...), redacted
    screens/NN-*.png  PII-masked screenshots
    dom/NN-*.html     redacted DOM snapshots (on failure / escalation)
    result.json       final result (discovery trace summary or ReplayResult)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from cua.safety.redact import Redactor

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def portable(path: str) -> str:
    """Repo-relative path when inside the project, so evidence is machine-independent."""
    ap = os.path.abspath(path)
    return os.path.relpath(ap, _ROOT) if ap.startswith(_ROOT + os.sep) else path


class Evidence:
    def __init__(self, run_dir: str, redactor: Redactor, run_id: str):
        self.dir = run_dir
        self.display_dir = portable(run_dir)
        self.redactor = redactor
        self.run_id = run_id
        self._seq = 0
        self._t0 = time.time()
        os.makedirs(os.path.join(run_dir, "screens"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "dom"), exist_ok=True)
        self._fh = open(os.path.join(run_dir, "events.jsonl"), "w")

    def event(self, type_: str, **data: Any) -> dict:
        self._seq += 1
        rec = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t_ms": int((time.time() - self._t0) * 1000),
            "run_id": self.run_id,
            "type": type_,
            **self.redactor.obj(data),
        }
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        return rec

    def _name(self, label: str, ext: str, sub: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)[:50]
        return os.path.join(self.dir, sub, f"{self._seq:03d}-{safe}.{ext}")

    def screenshot(self, surface, label: str) -> str | None:
        try:
            return os.path.relpath(surface.screenshot(self._name(label, "png", "screens")), self.dir)
        except Exception as e:  # evidence must never crash the run
            self.event("evidence_error", what="screenshot", error=str(e))
            return None

    def dom(self, surface, label: str) -> str | None:
        try:
            return os.path.relpath(surface.dom_snapshot(self._name(label, "html", "dom")), self.dir)
        except Exception as e:
            self.event("evidence_error", what="dom", error=str(e))
            return None

    def write_json(self, name: str, obj: Any) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            json.dump(self.redactor.obj(obj), f, indent=2, default=str)
        return path

    def close(self) -> None:
        self._fh.close()
