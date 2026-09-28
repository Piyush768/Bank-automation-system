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

A single Python process with four boundaries:

- **Surface.** Perception and action on one kind of UI (`surface/base.py`). Everything above it sees operator-level concepts: a control, the text beside it, a table row and column, a bounding box.
- **Agent + compiler.** The model decides. The compiler turns *what worked* into a contract.
- **Replay engine.** No model. It is condition-driven.
- **Control channel.** Records who owns the live session.

**Key decisions and trade-offs**

- **Perception is operator-level, not DOM-level.** The in-page script describes controls the way a human reads the screen: label-in-neighbouring-cell, row content, column header, "looks clickable" (so the `<span onclick>` is found even though the accessibility tree has no button there). It does not use IDs, test IDs or `<label for>`. The same description can come from a UIA tree or OCR on other surfaces. *Trade-off:* heuristics (header-row detection, nearest-label) can misread unusual layouts. Record-time validation (§3) catches that.
- **Text observation, not screenshots, for the model.** This is cheaper and every decision can be logged. The action space is typed (one forced `act` tool call per turn). Each call is stateless: it gets the full compact state plus the last 8 history lines, which bounds tokens and prevents drift. *Trade-off:* pure-pixel surfaces would need a vision observation. That would be a new Surface, not a new agent.
- **Synchronous single process.** One automation thread owns the browser. The operator console runs on another thread but only *queues* commands; the owning thread executes them. *Trade-off:* no horizontal scale, which is deliberately out of scope (§7).
- **Money is a decimal string** (`"14250.80"`), never a float.

## 2. Artifact schema

`cua/schema/artifact.py`. Example: `evidence/member-savings-balance.capability.json`.

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

**Why this shape**

- **It reads like a function signature.** `inputs → outputs | expected_outcomes` is exactly what a calling agent needs, and `catalog.py` turns it into a tool definition mechanically.
- **It is reviewable without the transcript.** Each step has a data-free `intent`, a human `description` of its target, a `risk`, and what we `expect` to see after it. The model's free-text reasoning stays in the discovery log, because it can mention names and balances.
- **Several locator strategies, ranked.** `label` (control right of "Member Number:") → `table_cell` (row containing X, column header Y) → `text` / `role` → `css` → `coords`. The surface-neutral kinds come first, and CSS and coordinates are late fallbacks. **Every strategy is validated at record time**: it is kept only if it resolves to exactly this element right now. Ambiguity is dropped when recording instead of discovered at replay.
- **Parameterised, never data-bearing.** Example values are templated everywhere, including inside locators. The link to open a member is `table_cell(row_contains="{{params.member_id}}", column="NAME")`, never the member's name. The compiler **refuses to emit** an artifact that still contains an example input, an extracted value, a secret, or a PII pattern.
- **Checkpoints are grounded.** They come from what was actually observed after each step: a new screen title, or text the model predicted *and* we verified. They are never taken from the model's say-so alone.
- **Approval is bound to `content_hash`.** Editing any behaviour-defining field voids approval. Replay refuses drafts unless explicitly allowed.

## 3. Determinism & error handling

**Determinism.** Same artifact + same inputs gives the same sequence of resolved controls and actions. Waiting is **condition-driven**, never a fixed sleep. After each action the engine polls until *either* the step's checkpoints hold, *or* a known condition from the app profile appears, *or* the step deadline passes.

**Locator resolution.** The engine tries the strategies in rank order. Each must match exactly one visible element of the expected control kind, so "pick the first match" is impossible. Using any strategy other than rank 0 is reported as `degraded` in the result's `locators` list. That list is the drift signal.

**Error taxonomy.** Conditions live in a per-vendor **profile** (`config/profiles/coreline-teller.json`). Error screens are a property of the vendor app, not of one flow, so every capability and tenant shares them. The page is checked against the profile *before* each resolution attempt. This means a missing control caused by "E-404 NO MEMBER" is reported as that outcome, not as a locator failure.

| Class | Examples | Result |
|---|---|---|
| business | `MEMBER_NOT_FOUND`, `ACCESS_DENIED`, `INVALID_MEMBER_NUMBER` | terminal `business_outcome`. It is an answer, not an error, and not retryable. |
| recoverable | `SYSTEM_NOTICE` (click CONTINUE), `SESSION_EXPIRED` / `HOST_ABEND` (restart the flow, budget 1) | handled in-run and listed in `recoveries` |
| fatal | `SIGNON_REJECTED` | terminal `failed`, never retried (retries would lock the account) |
| unknown | a screen not in the profile, or a checkpoint not reached | escalate to a human (§5); otherwise `failed` |

Restarting is only allowed while nothing irreversible has run. After a money-moving step, a session loss escalates instead of re-running. Inputs are validated against the schema *before* the UI is touched (`INPUT_INVALID`).

A **failure** carries: code, step id and intent, expected vs observed (landmarks, URL, per-strategy misses), a PII-masked screenshot, a redacted DOM snapshot, and `retryable`.

**Drift (secondary).** Scenario 11 replays tenant B's screens without its overlay. The CSS and coordinate fallbacks carry the flow forward, and each is flagged degraded. But the final checkpoint does not find the expected title, so the engine refuses to read a value off an unproven screen. Fallbacks buy resilience, and checkpoints stop that resilience from producing wrong answers.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The seam is the `Surface` interface: `observe`, `resolve(Target)`, `act`, `read`, `screenshot`. Locator kinds are defined in visual/operator terms, so they carry over:

- **Legacy web** (framesets, iframes): same Playwright surface, with observation recursing into frames and the frame path added to the handle.
- **Desktop** (`surface/desktop_stub.py`): the UIA tree gives Name, ControlType, LabeledBy/adjacent static text, DataGrid rows and headers, and BoundingRectangle. `label`, `table_cell`, `text` and `role` resolve the same way. `css` does not apply and is skipped.
- **Pixel-only** (Citrix): observation from OCR + layout, with `coords` as a first-class fallback.

The artifact schema, replay engine, error taxonomy and escalation don't change. Only `AppRef.surface` differs.

**Multi-tenant reuse.** There are three layers:

1. The **vendor profile**, shared by every tenant on `coreline-teller 4.2` (conditions, error codes).
2. The **canonical capability**, recorded once and holding paths, not hosts.
3. The **tenant binding** (`config/tenants/*.json`): base URL plus a text-override table for the institution's renamed menu items, labels, buttons and column headers.

At load time `specialise()` applies the overlay and logs every substitution. It refuses a vendor/version mismatch rather than guessing. Integrity and approval are checked on the canonical artifact. Scenario 10 runs the same artifact on "Lakeshore FCU" with five overrides.

**Detecting drift.** Per-tenant replay results record which strategy rank was used for each step. A tenant whose runs start using rank ≥1 for a step is drifting before it breaks. In production those signals would feed a re-validation queue, where the fix is usually a one-line overlay entry and not a re-recording. A version bump in the binding (`app_version`) forces an explicit decision instead of silent reuse.

## 5. Escalation & handoff

**Detecting "stuck".**
- *Discovery:* the model calls `escalate`, the same action is repeated on the same screen three times, three consecutive action errors occur, or the step budget runs out.
- *Replay:* no locator resolves by the deadline, a checkpoint is unmet with no known condition to explain it, or a restart is unsafe after an irreversible step.
- *Both:* an irreversible action needs approval.

**Control-transfer model** (`escalation/control.py`). The live session has exactly one owner: `AUTOMATION → AWAITING_OPERATOR → OPERATOR → AUTOMATION`. The surface calls `assert_can_act(actor)` before **every** action, so automation physically cannot click while a human holds the session, and the console cannot act until an operator claims it. The intervention request carries the capability or goal, step id and intent, expected vs observed, URL, which known condition (if any) matched, and a masked screenshot.

**Taking control of the same session.** The browser stays open. The operator console (`escalation/console.py`) shows a live, masked screenshot and accepts structured actions over HTTP. Those are queued to the thread that owns the browser, because Playwright sessions are single-threaded. With `--headed` the operator can also work directly in the real window. An injected listener captures their clicks and inputs while they hold control. Typed values are never recorded, only their length.

**Handing back.** The operator picks a mode:
- `resume_verify`: automation re-checks the expected state and continues
- `step_done` / `retry_step`
- `approve` / `deny`: for risky actions
- `abort`

Automation **never trusts** the handback: it re-verifies the step's checkpoints before moving on. The result records `human_assisted`, the operator, their actions and note. Scenario 09 shows the full loop against an unseen "supervisor override" screen.

**Human steps are not compiled automatically.** If a human helps during discovery, the artifact is stamped `operator_assisted` and a review note is added. Their actions are in the log but not turned into steps, because they are often judgment calls (override codes) that must stay human.

**What's minimal.** The console is a bare page, and escalation blocks the run's thread (`--operator none` turns "stuck" into an immediate debuggable failure; operator timeout returns `escalated`). The production design is below in §7.

## 6. Safety

**Allowlists.** `config/policy.toml` defines allowed hosts, path prefixes (with `/_harness` denied), and action types. They are enforced at three points:
1. the decision path, before acting;
2. the **network layer**: every request, including redirects and sub-resources the model never chose, goes through a route handler that aborts anything off-list;
3. the artifact's own guardrails, re-checked at replay.

**Risk classes.** An action's risk is set by what the committing control says:
- `safe`: typing, selecting, navigation, reads
- `reversible`: save, update
- `irreversible`: post, wire, delete, approve

Typing never commits, so it is always safe. Irreversible actions need a **single-use human approval** in both discovery and replay. I chose approval over blocking because blocking makes the system useless for real work, while flagging alone is too weak for money movement. Replay **re-classifies from the live control's text**, so a tampered artifact or a tenant renaming a button can't downgrade risk. Without an operator channel, irreversible steps fail closed (`POLICY_BLOCKED`). This is exercised live: the transfer capability was discovered by the model with the `POST TRANSFER` click paused for approval (evidence 12), and its replays show approve → posted once, deny → nothing posted, no operator → blocked (evidence 13–15).

**Data handling.**
- Credentials are secret *references*. The model never sees their values.
- The `Redactor` runs at every sink: event log, DOM snapshots, operator payloads, and a final pass over compiled artifacts. It keeps the last 4 digits of SSNs and Luhn-valid PANs, masks emails, and fully removes key/token/password values and any known secret.
- Screenshots are masked *before* the pixels reach disk.
- The model's observation has SSNs/PANs redacted (data minimisation).
- Artifacts contain no example inputs or extracted values; the compiler enforces this.

**Limits.**
- Regex redaction misses free-text PII such as names and addresses. That's why artifacts are built from chrome text only and model reasoning never enters them. Logs can still contain names.
- During discovery the model provider sees screen text. In production, discovery would run on a test tenant or synthetic records under a zero-retention agreement.
- Risk classification is keyword-based. A real deployment would add per-app risk annotations in the profile and a reviewer sign-off on every `reversible` or higher step.

## 7. Cuts

**Deliberately cut**

- **Real co-browsing operator UI.** It is a bare console plus live-window capture, but the ownership model and API are real.
- **Durable intervention queue.** Escalation blocks the process thread. In production the session would be parked (browser kept alive on a worker, CDP endpoint registered), the intervention persisted to a queue with SLAs and routing, and the run resumed by a worker when handed back.
- **Desktop and frames surfaces.** These are documented seams only.
- **Path minimisation in the compiler.** Every successful step is kept, so a model detour would be kept too. Reviewers see this, and a minimiser could replay-test step removal.
- **Vision observation** for pixel-only surfaces.
- **Secret store.** Secrets come from environment variables.

**Next, in order**

1. **Bounded assisted recovery.** On a single-step locator miss, one policy-checked LLM call proposes a replacement strategy. It is validated against record-time rules, used once, and queued as an artifact diff for review.
2. **Multi-run stability score** feeding the draft → approved gate.
3. **Cross-tenant canonicalisation.** Record on two tenants and auto-derive the overlay by aligning the two traces.
4. **Per-app risk annotations** and reviewer diffs between artifact versions.
