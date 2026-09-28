# Bank Automation System (CUA)

**CUA stands for Computer-Use Automation.**

This project shows how an AI model can learn a workflow in a legacy banking interface once, save that workflow as a reusable capability, and then run it again deterministically **without an AI model making decisions during replay**.

> **In simple terms:** the AI discovers the workflow once. The system saves it. Future runs follow the saved workflow.

This project uses a **local mock banking application with synthetic data only**. It does not connect to a real bank.

## What does it do?

The example task is simple: **look up a member and return their primary savings balance.**

The system works in three stages:

```text
Natural-language goal
        |
        v
1. LLM Discovery
   Observe -> Decide -> Act
        |
        v
2. Capability Artifact
   Typed + Versioned JSON
        |
        v
3. Deterministic Replay
   No LLM decisions
        |
        +--> Success + output
        +--> Business outcome
        +--> Recoverable condition
        +--> Hard failure
        +--> Human handoff
```

### 1. Discovery

An LLM operates the live banking UI. It observes the screen, decides what action to take, performs the action, and continues until the goal is complete.

### 2. Capability

A successful discovery run is converted into a structured JSON capability containing the inputs, outputs, ordered actions, UI locator strategies, checkpoints, safety rules, expected business outcomes, and version information.

The example capability is `capabilities/member-savings-balance.json`.

### 3. Replay

The saved capability can be executed again with a different member ID. **Replay does not ask an LLM what to do.** It follows the reviewed capability deterministically and verifies that the UI reaches the expected states.

### Example

Input:

```json
{
  "member_id": "10492"
}
```

Output:

```json
{
  "status": "success",
  "outputs": {
    "primary_savings_balance": "14250.80"
  }
}
```

## Key features

- Real LLM-driven observe -> decide -> act discovery
- Typed and versioned capability artifacts
- Deterministic replay without an LLM
- Multiple UI locator strategies with safe fallbacks
- Checkpoints that prevent incorrect results
- Structured business outcomes, recoverable conditions, and hard failures
- Human-in-the-loop escalation using the same live browser session
- Safety allowlists and action-risk controls
- Secret and sensitive-data redaction
- Cross-tenant capability reuse
- Screenshots and DOM evidence for debugging
- Automated tests

## Technology

- **Python 3.11**
- **Playwright + Chromium** for browser automation
- **Pydantic v2** for typed contracts
- **FastAPI** for the mock banking app and operator console
- **Anthropic, OpenAI, or Groq** for discovery

The LLM is used only during discovery. Deterministic replay does not require an LLM API key.

Design rationale is in [REPORT.md](REPORT.md). Run evidence is in [evidence/](evidence/).

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY. Optional: LLM_PROVIDER=anthropic
```

`.env` is git-ignored. Keys are read from the environment only and are also redacted from every log. `BANK_USERNAME` / `BANK_PASSWORD` in `.env` are the **fake** credentials of the local mock app. The model never sees them, only `{{secrets.username}}` / `{{secrets.password}}`.

## Run the demo

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

3. **A second discovery (real LLM) on an irreversible flow**: *"Transfer 25.00 from 10492-S01 to 10492-D10, post it, and return the confirmation number"*. When the model reaches `POST TRANSFER`, policy pauses it and a human approver must approve on the live session (dual control). It writes `capabilities/funds-transfer.json` and `evidence/12-discovery-transfer-dual-control/`. Then the transfer artifact is replayed:

| Scenario | Expected result |
|---|---|
| 13 approver approves | `success` with the confirmation number; exactly one transfer posted |
| 14 approver denies | `failed` `APPROVAL_DENIED`; nothing posted |
| 15 no operator attached | `failed` `POLICY_BLOCKED` (fails closed); nothing posted |

`evidence/README.md` is regenerated with the actual results. To re-run only the replays against an existing artifact, use `cua demo --skip-discovery`.

## Run each stage manually

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

## Tests and offline replay

- **Replay needs no LLM key.** It is the production path.
- **Tests run fully offline:** `python -m pytest` (38 tests, about 2 min, real headless browser against the mock app). They use `ScriptedProvider`, a clearly-marked **test-only** stand-in for the model, so the discovery loop, compiler and replay can be tested deterministically. It is never used for `evidence/`: the demo refuses to write evidence with it, and anything it produces is stamped `provider: scripted-test`.

## Project structure

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
