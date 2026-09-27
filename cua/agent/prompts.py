"""Prompt construction for the discovery agent."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from cua.surface.base import Observation


def _path(url: str) -> str:
    p = urlparse(url)
    return p.path + (("?" + p.query) if p.query else "")

SYSTEM = """You operate a legacy back-office banking application for a credit union, the way a trained human operator would. Your run is being RECORDED so it can be replayed later without you, so act deliberately and minimally.

Each turn you get the goal, the current screen, and your recent history. Reply with exactly ONE action via the `act` tool.

Rules:
- Only use element ids that appear in the current observation.
- To type a parameter, use its exact example value (e.g. the member number). To type a credential, use the placeholder {{secrets.NAME}}; you will never see real credentials.
- Some legacy controls are not real buttons (e.g. a <span> that acts like a button). If something looks clickable, it is.
- When a value the goal asks for is visible, use `extract` on the element holding just that value, with a snake_case output_name and an output_type (use "currency" for money).
- Take the most direct path. Do not explore unrelated screens.
- When every requested value is extracted and the goal is met, use `done` with a short summary.
- If you are stuck, blocked, or the next step needs a human decision, use `escalate` with a clear summary. Never guess credentials or override codes.
- If the app shows an error message, read it and react to it rather than repeating the same action."""


def build_user_prompt(
    goal: str,
    params: dict[str, str],
    secret_names: list[str],
    obs: Observation,
    history: list[str],
    outputs: dict[str, str],
    note: str | None = None,
) -> str:
    compact = {
        "url_path": _path(obs.url),
        "landmarks": obs.landmarks,
        "elements": [
            {k: v for k, v in {
                "id": e.idx,
                "kind": e.control,
                "tag": e.tag,
                "type": e.input_type,
                "label": e.label,
                "text": e.text,
                "value": e.value,
                "column": e.column_header,
                "row": e.row_text if e.column_header else "",
            }.items() if v not in ("", None)}
            for e in obs.elements
        ],
        "page_text": obs.page_text[:2500],
    }
    parts = [
        f"GOAL: {goal}",
        f"PARAMETERS (example values for this run): {json.dumps(params)}",
        f"CREDENTIALS AVAILABLE (by reference only): {', '.join('{{secrets.' + n + '}}' for n in secret_names) or 'none'}",
        f"EXTRACTED SO FAR: {json.dumps(outputs) if outputs else 'nothing yet'}",
        "RECENT HISTORY:\n" + ("\n".join(history[-8:]) if history else "(start)"),
    ]
    if note:
        parts.append(f"NOTE: {note}")
    parts.append("OBSERVATION_JSON:" + json.dumps(compact))
    return "\n\n".join(parts)
