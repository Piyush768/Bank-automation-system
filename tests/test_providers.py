"""Provider adapters parse the SDK responses correctly (SDK calls faked; no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from cua.agent import llm


def test_anthropic_adapter_forces_tool_and_parses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real-000000")
    p = llm.AnthropicProvider(model="claude-sonnet-5")
    seen = {}

    def create(**kw):
        seen.update(kw)
        return NS(content=[NS(type="text", text="x"), NS(type="tool_use", input={"thought": "t", "action": "click", "element": 3})],
                  usage=NS(input_tokens=10, output_tokens=5))

    p._client = NS(messages=NS(create=create))
    d = p.decide("sys", "user")
    assert d.action == "click" and d.element == 3 and d.usage["input_tokens"] == 10
    assert seen["tool_choice"] == {"type": "tool", "name": "act"} and seen["tools"][0]["name"] == "act"


def test_openai_adapter_forces_function_and_parses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0000000000")
    p = llm.OpenAIProvider(model="gpt-6-sol")
    seen = {}

    def create(**kw):
        seen.update(kw)
        return NS(output=[NS(type="reasoning"), NS(type="function_call", arguments=json.dumps({"thought": "t", "action": "done", "summary": "ok"}))],
                  usage=NS(input_tokens=7, output_tokens=2))

    p._client = NS(responses=NS(create=create))
    d = p.decide("sys", "user")
    assert d.action == "done" and d.summary == "ok"
    assert seen["tool_choice"] == {"type": "function", "name": "act"}


def test_groq_adapter_uses_chat_completions_tools(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
    p = llm.GroqProvider(model="llama-3.3-70b-versatile")
    seen = {}

    def create(**kw):
        seen.update(kw)
        call = NS(function=NS(name="act", arguments=json.dumps({"thought": "t", "action": "fill", "element": 2, "value": "10492"})))
        return NS(choices=[NS(message=NS(tool_calls=[call]))], usage=NS(prompt_tokens=11, completion_tokens=4))

    p._client = NS(chat=NS(completions=NS(create=create)))
    d = p.decide("sys", "user")
    assert d.action == "fill" and d.value == "10492" and d.usage["output_tokens"] == 4
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "act"}}
    assert "api.groq.com" in str(llm.GroqProvider()._client.base_url)


def test_make_provider_requires_a_key(monkeypatch):
    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="No LLM key"):
        llm.make_provider()
