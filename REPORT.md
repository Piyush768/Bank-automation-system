# Design report

## 1. Architecture

```
            DISCOVERY (once, LLM)                         PRODUCTION (many, no LLM)
 goal ─► DiscoveryAgent ─► Surface ◄─ live app ─► ReplayEngine ◄─ artifact + params + tenant
          observe→decide→act  │                      │  resolve → guard → act → await evidence
          trace ─► Compiler ──┴─► capability.json ───┘  │
                                   (review/approve)      ├─ success(outputs) │ business_outcome
 Policy + Redactor wrap every step; ControlChannel       ├─ failed(debug bundle)
 + operator console can take over the live session ─────┴─ escalate → human → hand back → verify
```

One Python process, four boundaries: the **Surface** (perceive and act on one kind of UI), the **agent + compiler** (the model decides; the compiler turns what worked into a contract), the **replay engine** (no model), and the **control channel** (who owns the live session).

**Key decisions and trade-offs**

- **Perception is operator-level, not DOM-level.** Controls are described as a person reads them: the text in the neighbouring cell, the row's content, the column header, "looks clickable" (so a `<span onclick>` with no button role is still found). No IDs, test IDs or `<label for>` are needed, and the same description can come from a UIA tree or OCR. *Trade-off:* layout heuristics can misread unusual screens; record-time validation (§2) catches that.
- **Text observation, one forced tool call per turn, stateless.** Each call gets the compact screen plus the last 8 history lines: cheap, bounded, every decision logged. *Trade-off:* pixel-only surfaces need a vision observation, which is a new Surface, not a new agent.
- **Synchronous single process.** One thread owns the browser; the operator console only queues commands to it. *Trade-off:* no horizontal scale, deliberately out of scope (§7).
- **Money is a decimal string** (`"14250.80"`), never a float.

## 2. Artifact schema

`cua/schema/artifact.py`; examples in `evidence/*.capability.json`.

```
Capability  schema="cua.capability/v1", id, version, title, description, content_hash
  app       vendor_app, app_version, profile (condition catalog), surface
  entry_path            (path only; base URL comes from the tenant binding)
  inputs[]  name, type, pattern, inferred        ← agent supplies per call
  secrets[] name                                  ← injected at runtime, never stored
  outputs[] name, type (currency|number|date…), source: Target
  expected_outcomes[]  code, description          ← legitimate non-success answers
  steps[]   id, action, intent, target, value ("{{params.x}}"/"{{secrets.y}}"), risk, expect[], origin
  success[] checkpoints for the final state
  guardrails  allowed_actions, max_risk, irreversible_requires_approval
  provenance  discovery run, provider/model, goal, compiler version, operator_assisted
  review      draft|approved|deprecated, approved_by, approved_hash
Target      description, control, strategies[] (ranked)
```

- **Reads like a function signature.** `inputs → outputs | expected_outcomes` is what a calling agent needs; `catalog.py` turns it into a tool definition mechanically.
- **Reviewable without the transcript.** Each step has a data-free `intent`, a human `description` of its target, a `risk`, and what we `expect` afterwards. The model's free-text reasoning stays in the log, because it can mention names and balances.
- **Ranked locators, validated at record time.** `label` → `table_cell` (row containing X, column Y) → `text` / `role` → `css` → `coords`: surface-neutral kinds first. A strategy is kept only if it resolves to exactly this element when recorded, so ambiguity is dropped at record time, not discovered at replay.
- **Parameterised, never data-bearing.** Example values are templated everywhere, including inside locators: the member link is `table_cell(row_contains="{{params.member_id}}", column="NAME")`, never the member's name. The compiler **refuses to emit** an artifact containing an example input, an extracted value, a secret or a PII pattern.
- **Grounded checkpoints.** Only text actually observed after a step (a new screen title, or a prediction we verified), never the model's say-so.
- **Approval is bound to `content_hash`.** Editing any behaviour-defining field voids it; replay refuses drafts unless explicitly allowed.

## 3. Determinism & error handling

**Determinism.** Same artifact + same inputs gives the same resolved controls and actions. Waiting is condition-driven, never a fixed sleep: after each action the engine polls until the checkpoints hold, a known condition appears, or the deadline passes.

**Locators.** Strategies are tried in rank order; each must match exactly one visible element of the expected kind, so "pick the first match" is impossible. Any strategy below rank 0 is reported `degraded`: the drift signal.

**Error taxonomy.** Conditions live in a per-vendor **profile** (`config/profiles/coreline-teller.json`): error screens belong to the app, so every capability and tenant shares them. The page is checked against the profile *before* each resolution attempt, so a control missing because of "E-404 NO MEMBER" is reported as that outcome, not a locator failure.

| Class | Examples | Result |
|---|---|---|
| business | `MEMBER_NOT_FOUND`, `ACCESS_DENIED`, `INVALID_MEMBER_NUMBER` | terminal `business_outcome`. It is an answer, not an error, and not retryable. |
| recoverable | `SYSTEM_NOTICE` (click CONTINUE), `SESSION_EXPIRED` / `HOST_ABEND` (restart the flow, budget 1) | handled in-run and listed in `recoveries` |
| fatal | `SIGNON_REJECTED` | terminal `failed`, never retried (retries would lock the account) |
| unknown | a screen not in the profile, or a checkpoint not reached | escalate to a human (§5); otherwise `failed` |

Restart is allowed only while nothing irreversible has run; after a money-moving step a session loss escalates instead. Inputs are validated before the UI is touched (`INPUT_INVALID`). A **failure** carries the code, step and intent, expected vs observed (landmarks, URL, per-strategy misses), a masked screenshot, a redacted DOM snapshot, and `retryable`.

**Drift.** Scenario 11 replays tenant B without its overlay: CSS and coordinate fallbacks carry the flow (each flagged degraded), but the final checkpoint does not find the expected title, so no value is read off an unproven screen. Fallbacks buy resilience; checkpoints stop it producing wrong answers.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The seam is the `Surface` interface (`observe`, `resolve(Target)`, `act`, `read`, `screenshot`). Locators are defined in operator terms, so they carry over. Legacy web with frames reuses the Playwright surface, recursing into frames. A desktop app (`surface/desktop_stub.py`) maps UIA Name, ControlType, LabeledBy, DataGrid headers and BoundingRectangle onto the same `label`, `table_cell`, `text` and `role` strategies (`css` is skipped). Pixel-only surfaces would observe via OCR with `coords` as a first-class fallback. The schema, replay engine, taxonomy and escalation are unchanged; only `AppRef.surface` differs.

**Multi-tenant reuse**, in three layers: the **vendor profile** (shared conditions), the **canonical capability** (recorded once, paths not hosts), and a **tenant binding** (`config/tenants/*.json`: base URL plus text overrides for renamed menus, labels, buttons and headers). `specialise()` applies the overlay at load time, logs every substitution, and refuses a vendor/version mismatch. Integrity and approval are checked on the canonical artifact. Scenario 10 runs the same artifact at "Lakeshore FCU" with five overrides.

**Drift management.** Results record the strategy rank used per step. A tenant whose runs slip to rank ≥1 is drifting before it breaks; that signal would feed a re-validation queue where the fix is usually one overlay line, not a re-recording. An `app_version` bump forces an explicit decision instead of silent reuse.

## 5. Escalation & handoff

**Detecting "stuck".** In discovery: the model calls `escalate`, repeats the same action on the same screen three times, hits three consecutive errors, or exhausts its step budget. In replay: no locator resolves by the deadline, a checkpoint is unmet with no known condition, or a restart would be unsafe. In both: an irreversible action needs approval.

**Control-transfer model** (`escalation/control.py`). The session has exactly one owner: `AUTOMATION → AWAITING_OPERATOR → OPERATOR → AUTOMATION`. The surface calls `assert_can_act(actor)` before **every** action, so automation cannot click while a human holds the session, and the console cannot act until an operator claims it. The request carries the capability or goal, step and intent, expected vs observed, URL, any matched condition, and a masked screenshot.

**Same live session.** The browser stays open. The console (`escalation/console.py`) shows a live masked screenshot and accepts actions over HTTP, queued to the thread that owns the browser. With `--headed` the operator can use the real window; an injected listener records their clicks and inputs (typed values only as lengths).

**Handing back.** Modes: `resume_verify`, `step_done`, `retry_step`, `approve` / `deny`, `abort`. Automation never trusts the handback: it re-verifies checkpoints before moving on, and the result records `human_assisted`, the operator, their actions and note (scenario 09). Human actions during discovery are logged but not compiled into steps (the artifact is stamped `operator_assisted`), because they are often judgment calls that must stay human. The console is deliberately bare and escalation blocks the run's thread; `--operator none` turns "stuck" into an immediate debuggable failure, and operator timeout returns `escalated`.

## 6. Safety

**Allowlists** (`config/policy.toml`: hosts, path prefixes with `/_harness` denied, action types) are enforced at three points: the decision path; the **network layer**, where every request (including redirects and sub-resources the model never chose) is aborted if off-list; and the artifact's guardrails, re-checked at replay.

**Risk classes** come from what the committing control says: `safe` (typing, selecting, navigation, reads), `reversible` (save, update), `irreversible` (post, wire, delete, approve). Irreversible actions need a **single-use human approval** in discovery and replay; I chose approval over blocking because blocking makes the system useless, while flagging is too weak for money movement. Replay **re-classifies from the live control's text**, so a tampered artifact or a renamed button cannot downgrade risk; with no operator channel it fails closed (`POLICY_BLOCKED`). This is exercised live: the transfer was discovered with `POST TRANSFER` paused for approval (evidence 12), and replays show approve → posted once, deny → nothing posted, no operator → blocked (13–15).

**Data handling.** Credentials are secret references the model never sees. The `Redactor` runs at every sink (logs, DOM snapshots, operator payloads, a final pass over artifacts): last 4 digits of SSNs and Luhn-valid PANs kept, emails masked, keys, tokens, passwords and known secrets removed. Screenshots are masked before reaching disk, the model's observation has SSNs/PANs redacted, and artifacts carry no inputs or extracted values.

**Limits.**
- Regex redaction misses free-text PII such as names and addresses. That's why artifacts are built from chrome text only and model reasoning never enters them. Logs can still contain names.
- During discovery the model provider sees screen text. In production, discovery would run on a test tenant or synthetic records under a zero-retention agreement.
- Risk classification is keyword-based. A real deployment would add per-app risk annotations in the profile and a reviewer sign-off on every `reversible` or higher step.

## 7. Cuts

**Deliberately cut:** a real co-browsing operator UI (the ownership model and API are real); a durable intervention queue (in production the session would be parked on a worker with its CDP endpoint registered, the intervention queued with SLAs, and the run resumed on handback); desktop and frames surfaces (documented seams); path minimisation (every successful step is kept, so a model detour would be too; reviewers see it); vision observation; a real secret store (secrets come from the environment).

**Next, in order:**
1. Bounded assisted recovery: on a single-step miss, one policy-checked LLM call proposes a new locator, used once and queued as a diff for review.
2. A multi-run stability score feeding the draft → approved gate.
3. Cross-tenant canonicalisation: derive overlays by aligning recordings from two tenants.
4. Per-app risk annotations and reviewer diffs between artifact versions.
