"""LLM providers for DISCOVERY. Replay never imports this module.

Each call is stateless: the agent sends the full (compact, redacted) state
every turn and forces a single structured tool call (``act``). That keeps
token use bounded, makes every decision independently loggable, and avoids a
growing conversation drifting away from what is actually on screen.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

ACTIONS = ["click", "fill", "select", "press", "extract", "done", "escalate"]

ACT_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string", "description": "One or two sentences: why this action moves toward the goal."},
        "action": {"type": "string", "enum": ACTIONS},
        "element": {"type": "integer", "description": "Element id from the observation (required for click/fill/select/press/extract)."},
        "value": {"type": "string", "description": "fill/select: text to enter. Use {{secrets.NAME}} for credentials. press: key name, e.g. Enter."},
        "output_name": {"type": "string", "description": "extract: snake_case name of the output field."},
        "output_type": {"type": "string", "enum": ["string", "number", "currency", "integer", "date"]},
        "expect_text": {"type": "string", "description": "Short text you expect on screen right after this action succeeds, if you can tell (e.g. a page title)."},
        "summary": {"type": "string", "description": "done/escalate: result summary or why you are stuck."},
    },
    "required": ["thought", "action"],
}

TOOL_DESCRIPTION = "Perform exactly one action on the current screen."


@dataclass
class Decision:
    raw: dict
    usage: dict

    def __getattr__(self, k):
        return self.raw.get(k)


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def decide(self, system: str, user: str) -> Decision: ...


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic

        self.model = model or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def decide(self, system: str, user: str) -> Decision:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": "act", "description": TOOL_DESCRIPTION, "input_schema": ACT_SCHEMA}],
            tool_choice={"type": "tool", "name": "act"},
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        return Decision(dict(block.input), usage)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str | None = None):
        import openai

        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-6-sol"
        self._client = openai.OpenAI()  # reads OPENAI_API_KEY

    def decide(self, system: str, user: str) -> Decision:
        resp = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=user,
            tools=[{"type": "function", "name": "act", "description": TOOL_DESCRIPTION, "parameters": ACT_SCHEMA}],
            tool_choice={"type": "function", "name": "act"},
        )
        call = next(o for o in resp.output if getattr(o, "type", "") == "function_call")
        u = getattr(resp, "usage", None)
        usage = {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None)}
        return Decision(json.loads(call.arguments), usage)


class GroqProvider(LLMProvider):
    """Groq (open models, OpenAI-compatible Chat Completions API with tool calling)."""

    name = "groq"

    def __init__(self, model: str | None = None):
        import openai

        self.model = model or os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"
        self._client = openai.OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")

    def decide(self, system: str, user: str) -> Decision:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=800,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[{"type": "function", "function": {"name": "act", "description": TOOL_DESCRIPTION, "parameters": ACT_SCHEMA}}],
            tool_choice={"type": "function", "function": {"name": "act"}},
        )
        call = resp.choices[0].message.tool_calls[0]
        u = getattr(resp, "usage", None)
        usage = {"input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None)}
        return Decision(json.loads(call.function.arguments), usage)


class ScriptedProvider(LLMProvider):
    """TEST-ONLY stand-in that picks elements with simple rules.

    It exists so the test-suite can exercise the discovery loop and compiler
    without network access. It is NOT a discovery engine: the CLI refuses to
    write evidence/discovery with it, and artifacts it produces are stamped
    provider="scripted-test".
    """

    name = "scripted-test"
    model = "rules"

    def __init__(self, plan: list[dict]):
        self._plan = list(plan)

    def decide(self, system: str, user: str) -> Decision:
        obs = json.loads(user.split("OBSERVATION_JSON:", 1)[1])
        step = self._plan.pop(0)
        out = {k: v for k, v in step.items() if k not in ("find",)}
        if "find" in step:
            f = step["find"]
            for e in obs["elements"]:
                if all(f[k].lower() in str(e.get(k, "")).lower() for k in f):
                    out["element"] = e["id"]
                    break
            else:
                raise AssertionError(f"scripted provider could not find {f}")
        out.setdefault("thought", "scripted")
        return Decision(out, {"input_tokens": 0, "output_tokens": 0})


def make_provider(name: str | None = None) -> LLMProvider:
    """Choose a provider: explicit name > LLM_PROVIDER > whichever key is set."""
    name = (name or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not name:
        if os.getenv("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            name = "openai"
        elif os.getenv("GROQ_API_KEY"):
            name = "groq"
        else:
            raise RuntimeError(
                "No LLM key found. Discovery needs a real model: put ANTHROPIC_API_KEY, "
                "OPENAI_API_KEY or GROQ_API_KEY in .env (see .env.example). Replay does not need a key."
            )
    if name == "anthropic":
        return AnthropicProvider()
    if name == "openai":
        return OpenAIProvider()
    if name == "groq":
        return GroqProvider()
    raise ValueError(f"unknown provider {name}")
