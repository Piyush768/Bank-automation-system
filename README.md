# Computer-Use Automation for legacy banking UIs

An LLM operates a legacy back-office app **once** to reach a goal. That run is compiled into a typed, versioned, reviewable **capability artifact**. The artifact then **replays deterministically with no model in the loop**. Replay returns typed outputs, a business outcome, or a debuggable failure, and hands the live session to a human when it cannot safely continue.

```
goal ──► discovery (LLM: observe → decide → act) ──► artifact (JSON contract) ──► replay (no LLM)
                                                                                    │
                                     success(outputs) · business_outcome · failed · escalated → human
```

- **Language / stack:** Python 3.11, Playwright (Chromium), Pydantic v2, FastAPI (mock app + operator console).
- **LLM (discovery only):** Anthropic Claude, OpenAI, or Groq (open models), switchable. Replay never imports the LLM module.
- **Target:** `mock_bank/`, *CoreLine Teller 4.2*, a deliberately hostile legacy app. It is table-based with no IDs, no `<label>`s, and no test IDs. Its search button is a `<span onclick>` with no button role, and table row order differs per member. It injects real runtime conditions and has a second tenant skin. All data is synthetic.

Design rationale is in [REPORT.md](REPORT.md). Evidence from runs is in [evidence/](evidence/).

---

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY, OPENAI_API_KEY or GROQ_API_KEY. Optional: LLM_PROVIDER=anthropic|openai|groq
```

`.env` is git-ignored. Keys are read from the environment only and are also redacted from every log. `BANK_USERNAME` / `BANK_PASSWORD` in `.env` are the **fake** credentials of the local mock app. The model never sees them, only `{{secrets.username}}` / `{{secrets.password}}`.

## Demo path (one command)

```bash
cua demo
```

This starts two mock-app tenants (ports 8600/8601) and then:

1. **Discovery (real LLM)** on the goal *"Look up member 10492 and read their current primary savings balance"*. It writes `capabilities/member-savings-balance.json` and the discovery log in `evidence/01-discovery/`.
2. **Replays of that artifact** with different inputs and injected runtime conditions. No LLM is used. Each writes `evidence/NN-*/`:

| Scenario | Expected result |
|---|---|
| 02 member 10492 | `success`, `primary_savings_balance: "14250.80"` |
| 03 member 20318 (savings is in row 2) | `success`, `"3402.17"`: cell addressed by content, not position |
| 04 member 99999 | `business_outcome` `MEMBER_NOT_FOUND` |
| 05 member 10007 | `business_outcome` `ACCESS_DENIED` |
| 06 `member_id=12AB` | `failed` `INPUT_INVALID`, rejected before touching the UI |
| 07 notice + session expiry + slow host | `success` with `recoveries: [SYSTEM_NOTICE, SESSION_EXPIRED]` |
| 08 host ABEND that persists | `failed` `APP_ERROR` (after one restart) with screenshot + DOM |
| 09 unknown "supervisor override" screen | escalated, then operator takes the live session and hands back; result verified `success`, `human_assisted: true` |
| 10 second tenant (renamed labels) | `success` via tenant overlay, no re-recording |
| 11 second tenant **without** overlay | `failed`; degraded locators flagged, checkpoint refuses the wrong screen |

`evidence/README.md` is regenerated with the actual results. To re-run only the replays against an existing artifact, use `cua demo --skip-discovery`.

## Step by step

```bash
# 1. the target app (tenant a on :8600; `--tenant b --port 8601` for the second institution)
cua app

# 2. discovery: the LLM drives the UI; an operator console opens at http://127.0.0.1:8765
cua discover \
  --goal "Look up member 10492 and read their current primary savings balance" \
  --start-url http://127.0.0.1:8600/signon --param member_id=10492 \
  --id member-savings-balance --out capabilities/member-savings-balance.json \
  --evidence runs/discovery --headed

# 3. review + approve (approval is bound to the artifact's content hash)
cua approve --capability capabilities/member-savings-balance.json --by "your-name"

# 4. deterministic replay (no key needed)
cua replay --capability capabilities/member-savings-balance.json \
  --param member_id=20318 --tenant config/tenants/harbor-point.json
cua replay --capability capabilities/member-savings-balance.json \
  --param member_id=99999 --tenant config/tenants/harbor-point.json      # business outcome

# 5. same artifact, second institution (start `cua app --tenant b --port 8601` first)
cua replay --capability capabilities/member-savings-balance.json \
  --param member_id=20318 --tenant config/tenants/lakeshore.json

# 6. agent-facing catalog (stretch): approved artifacts as tool definitions, invoked by name
cua catalog
cua invoke member_savings_balance --args '{"member_id":"10492"}' --tenant config/tenants/harbor-point.json
```

Replay of an unapproved artifact is refused (`NOT_APPROVED`) unless you pass `--allow-draft`.

### Human-in-the-loop by hand

Inject the unknown screen, then replay with a console attached:

```bash
curl -X POST localhost:8600/_harness/faults -H 'content-type: application/json' -d '{"supervisor_once": true}'
cua replay --capability capabilities/member-savings-balance.json --param member_id=10492 \
  --tenant config/tenants/harbor-point.json --operator console --headed
```

When the run escalates, open http://127.0.0.1:8765. Then:
1. Click **Take control**.
2. Either use the console (fill *OVERRIDE CODE* = `4242`, click `ACKNOWLEDGE`) or work directly in the visible browser window. Both are captured.
3. **Hand back** with *resume: verify state and continue*.

Automation re-verifies the expected screen before continuing.

Fault-injection switches (mock app only; the agent's policy denies `/_harness`): `slow_ms`, `interstitial_once`, `expire_once`, `errors_remaining`, `supervisor_once`, `session_ttl_s`.

## Running without live services

- **Replay needs no LLM key.** It is the production path.
- **Tests run fully offline:** `pytest` (37 tests, about 2 min, real headless browser against the mock app). They use `ScriptedProvider`, a clearly-marked **test-only** stand-in for the model, so the discovery loop, compiler and replay can be tested deterministically. It is never used for `evidence/`: the demo refuses to write evidence with it, and anything it produces is stamped `provider: scripted-test`.

## Layout

```
cua/
  agent/       discovery loop, prompts, LLM providers (Anthropic/OpenAI + test-only scripted)
  compiler.py  trace → capability artifact (templating, grounded checkpoints, leak checks)
  schema/      artifact.py (the capability contract), results.py (the replay result contract)
  surface/     Surface seam; browser.py (Playwright); dom_scripts.py (perception + locator resolution);
               desktop_stub.py (documented seam)
  replay/      engine.py: deterministic replay, condition handling, recovery, escalation
  escalation/  control.py (ownership model), handoff.py (pause/resume), console.py (operator UI + API)
  safety/      policy.py (allowlists, risk classes), redact.py (PII/secret redaction)
  tenancy.py   vendor-app condition profiles + tenant overlays
  catalog.py   approved artifacts → agent tool definitions (stretch)
config/        policy.toml, profiles/coreline-teller.json, tenants/*.json
mock_bank/     the hostile legacy target app (+ fault injection)
tests/         unit + end-to-end tests
evidence/      curated run evidence (generated by `cua demo`)
```
