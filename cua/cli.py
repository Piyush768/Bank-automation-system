"""Command-line entry point: `cua <command>` (or `python -m cua.cli <command>`)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from cua import runtime


def _params(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not k or not _:
            raise SystemExit(f"--param expects name=value, got {p!r}")
        out[k] = v
    return out


def cmd_app(a):
    import uvicorn

    from mock_bank.app import create_app

    print(f"CoreLine Teller mock (tenant {a.tenant}) on http://127.0.0.1:{a.port}/signon")
    uvicorn.run(create_app(a.tenant), host="127.0.0.1", port=a.port, log_level="warning")


def cmd_discover(a):
    cap, summary = runtime.discover(
        goal=a.goal, start_url=a.start_url, params=_params(a.param), capability_id=a.id,
        title=a.title or a.id.replace("-", " "), out_path=a.out, evidence_dir=a.evidence,
        operator=a.operator, headed=a.headed, provider=None if not a.provider else _provider(a.provider),
        max_steps=a.max_steps,
    )
    runtime.print_json(summary)
    return 0 if cap else 1


def _provider(name):
    from cua.agent.llm import make_provider

    return make_provider(name)


def cmd_replay(a):
    from cua.schema.artifact import Capability

    cap = Capability.load(a.capability)
    res = runtime.replay(capability=cap, params=_params(a.param), tenant_path=a.tenant, base_url=a.base_url,
                         evidence_dir=a.evidence, operator=a.operator, headed=a.headed,
                         allow_draft=a.allow_draft, step_timeout_s=a.step_timeout)
    runtime.print_json(res.model_dump(mode="json", exclude_none=True))
    return {"success": 0, "business_outcome": 0}.get(res.status, 2)


def cmd_approve(a):
    from cua.schema.artifact import Capability

    cap = Capability.load(a.capability)
    if cap.compute_hash() != cap.content_hash:
        raise SystemExit("content_hash mismatch: artifact was edited; recompile or re-hash deliberately")
    cap.review.status = "approved"
    cap.review.approved_by = a.by
    cap.review.approved_at = datetime.now(timezone.utc)
    cap.review.approved_hash = cap.content_hash
    if a.notes:
        cap.review.notes = a.notes
    cap.save(a.capability)
    print(f"approved {cap.id} {cap.version} ({cap.content_hash}) by {a.by}")


def cmd_catalog(a):
    from cua.catalog import load_catalog, tool_definition

    caps = load_catalog(a.dir, include_drafts=a.include_drafts)
    runtime.print_json([tool_definition(c) for c in caps.values()])


def cmd_invoke(a):
    from cua.catalog import load_catalog

    caps = load_catalog(a.dir, include_drafts=a.allow_draft)
    if a.name not in caps:
        raise SystemExit(f"no {'' if a.allow_draft else 'approved '}capability named {a.name}; have {sorted(caps)}")
    res = runtime.replay(capability=caps[a.name], params=json.loads(a.args), tenant_path=a.tenant, base_url=None,
                         evidence_dir=a.evidence, allow_draft=a.allow_draft)
    runtime.print_json(res.model_dump(mode="json", exclude_none=True))


def cmd_demo(a):
    from cua.demo import run_demo

    rows = run_demo(skip_discovery=a.skip_discovery)
    print("\nSummary:")
    for r in rows:
        print(f"  {r['scenario']:<40} {r['status']:<17} {r['detail']}")
    print("\nEvidence written to evidence/ (see evidence/README.md)")


def main(argv=None):
    runtime.load_env()
    p = argparse.ArgumentParser(prog="cua", description="Computer-use automation for legacy banking UIs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("app", help="run the mock legacy bank app")
    s.add_argument("--port", type=int, default=8600)
    s.add_argument("--tenant", choices=["a", "b"], default="a")
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("discover", help="LLM-driven discovery -> capability artifact")
    s.add_argument("--goal", required=True)
    s.add_argument("--start-url", required=True)
    s.add_argument("--param", action="append", help="name=example_value (repeatable)")
    s.add_argument("--id", required=True, help="capability id, e.g. member-savings-balance")
    s.add_argument("--title")
    s.add_argument("--out", required=True)
    s.add_argument("--evidence", default="runs/discovery")
    s.add_argument("--operator", choices=["none", "console"], default="console")
    s.add_argument("--headed", action="store_true", help="show the browser (operator can use it directly)")
    s.add_argument("--provider", choices=["anthropic", "openai", "groq"])
    s.add_argument("--max-steps", type=int, default=25)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("replay", help="deterministic replay of an artifact (no LLM)")
    s.add_argument("--capability", required=True)
    s.add_argument("--param", action="append")
    s.add_argument("--tenant", help="tenant binding JSON (base URL + overlay)")
    s.add_argument("--base-url")
    s.add_argument("--evidence", default="runs/replay")
    s.add_argument("--operator", choices=["none", "console"], default="none")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--allow-draft", action="store_true", help="permit replaying an unapproved artifact")
    s.add_argument("--step-timeout", type=float, default=8.0)
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("approve", help="mark an artifact approved (binds approval to its content hash)")
    s.add_argument("--capability", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("catalog", help="list approved capabilities as agent tool definitions")
    s.add_argument("--dir", default="capabilities")
    s.add_argument("--include-drafts", action="store_true")
    s.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("invoke", help="invoke a catalog capability by tool name with JSON args")
    s.add_argument("name")
    s.add_argument("--args", required=True)
    s.add_argument("--tenant", required=True)
    s.add_argument("--dir", default="capabilities")
    s.add_argument("--evidence", default="runs/invoke")
    s.add_argument("--allow-draft", action="store_true")
    s.set_defaults(fn=cmd_invoke)

    s = sub.add_parser("demo", help="full end-to-end demo; writes evidence/")
    s.add_argument("--skip-discovery", action="store_true", help="reuse capabilities/member-savings-balance.json")
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except RuntimeError as e:  # e.g. no LLM key: a clear message, not a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
