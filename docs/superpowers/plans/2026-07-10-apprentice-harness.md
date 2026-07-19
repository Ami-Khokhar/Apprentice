# Apprentice — OpenAI Build Week Implementation Plan

**Date:** 2026-07-10  
**Status:** Ready for implementation; replaces the original 16-task plan  
**Target:** OpenAI Build Week, July 13–21, 2026  
**Goal:** Deliver one polished, runnable vertical slice proving that an agent can learn a browser task from demonstrations, earn autonomy from non-replayable evidence, and rehearse the exact production action plan in an offline twin before any production side effect.

**Design spec:** docs/superpowers/specs/2026-07-10-apprentice-harness-design.md

> **Execution rule:** Implement the tasks below in order. Use test-first development, keep changes within the task's file list, and finish every task with uv run pytest -q. Do not perform git operations or commits.

---

## 1. Build Week strategy

The submission is optimized for the published judging criteria:

1. **Technological implementation:** GPT-5.6 performs semantic induction and agentic rehearsal; deterministic code owns authorization, invariants, and production effects.
2. **Design:** one coherent dashboard presents the capability ladder, rehearsal evidence, approval state, and outcome timeline.
3. **Potential impact:** the demo shows a credible path from supervised automation to revocable autonomy for repetitive browser work.
4. **Quality of idea:** trust is earned per capability and every production action plan is rehearsed before it can create a side effect.

The full challenge details are expected when submissions open on July 13. Re-check the rules, tracks, required materials, and video limits before Task 8.

### Product sentence

**Apprentice watches you perform a task, turns the demonstrations into a semantic playbook, rehearses each new run in an offline twin, and earns narrowly scoped autonomy from verified evidence.**

### Demo promise

The demo must prove all of the following:

- Two demonstrations produce a typed, inspectable playbook.
- GPT-5.6 handles a held-out example that was not used for induction.
- A new run produces a stored action plan and passing invariant report in the twin.
- Production executes only that exact stored plan.
- L2 pauses for explicit approval; L3 uses a durable veto countdown.
- A meaningful site change fails rehearsal.
- A failed rehearsal leaves the production mutation counter unchanged.
- The ledger demotes the affected capability and shows why.

---

## 2. Scope

### Build for the submission

- Single user and single local machine.
- One self-hosted expense portal.
- Recording begins after login; authentication capture is out of scope.
- Two training demonstrations plus at least one held-out demonstration.
- GPT-5.6 playbook induction with typed output.
- GPT-5.6 agent execution inside the twin.
- Sanitized trace, screenshot, and HTTP snapshot artifacts.
- Sidecar-owned run state, rehearsal records, verdicts, vetoes, and trust events.
- Exact action-plan replay in production.
- L0–L4 data model, with L1–L3 exercised in the demo.
- One polished dashboard plus a run detail/timeline view.
- Automated end-to-end coverage of the complete demo.
- OpenAI Agents SDK traces and a small repeatable evaluation corpus.

### Explicitly defer

- Arbitrary self-authored Python tools.
- OS-level sandboxing.
- LLM-generated critical-path HTTP responses.
- Multi-user or cloud deployment.
- Chrome extension and everyday-browser recording.
- General authenticated-site recording.
- Full-desktop computer use.
- Multi-domain support.
- L4 audit scheduling.
- A five-page administration console.

### Stretch only after the complete demo is green

- A declarative, read-only HTTP tool manifest authored by GPT-5.6 and executed by the sidecar.
- A second demo domain.
- OpenAI-hosted trace graders in addition to the local eval corpus.

---

## 3. Non-negotiable invariants

1. The caller cannot assert that a run was rehearsed. The sidecar verifies a stored passing rehearsal.
2. A rehearsal is bound to the capability, playbook version, canonical input digest, tool versions, and ordered action-plan hash.
3. Production executes only the stored ordered action plan. A mismatched action, argument, host, request method, or commit payload aborts the run.
4. The twin never forwards a mutating request to the target.
5. Unknown requests and unseen hosts fail closed.
6. A response synthesized without an observed template can never satisfy a critical-path rehearsal.
7. A failed rehearsal causes zero production mutations.
8. Only trusted sidecar workflows create ledger events. There is no public endpoint accepting arbitrary event type or weight.
9. Evidence is non-replayable. Each evidence key is unique.
10. Veto and approval state survives an agent or sidecar restart.
11. Password and OTP sentinel values are absent from every retained trace, screenshot reference, and HTTP artifact.
12. A major failure demotes one level immediately. A critical failure demotes two levels immediately.
13. Unknown actions and unknown capability/tool buckets default to deny.
14. User-facing failures return typed results or HTTP errors; expected failures do not escape as uncaught exceptions.

---

## 4. Technical choices

- Python 3.12.
- uv for environment and command execution.
- FastAPI, Uvicorn, Jinja2, and sse-starlette.
- SQLite through stdlib sqlite3; no ORM.
- Playwright Python.
- OpenAI Agents SDK for GPT-5.6 induction/rehearsal and approval interruptions.
- Pydantic models for all model outputs and API contracts.
- pytest, pytest-asyncio, httpx, and Ruff.
- Model name from APPRENTICE_MODEL, default gpt-5.6.
- Tests inject fake model runners; normal CI never requires an API key.
- Tests inject clocks; countdown tests never sleep.
- Async workflows use async HTTP clients or direct service calls, not blocking HTTP calls inside the event loop.
- Every SQLite operation uses a repository-managed connection/transaction. Do not share one unprotected connection across request threads.

Dependencies must include python-multipart because the portal accepts form data and file uploads.

---

## 5. Architecture

~~~text
Recorded demonstrations
        │
        ▼
GPT-5.6 induction agent ──► typed, reviewed Playbook vN
        │
        ▼
Sidecar creates RunSpec
  capability + version + canonical input digest
        │
        ▼
read-only current-site snapshot + recorded HTTP templates
        │
        ▼
GPT-5.6 rehearsal agent inside Twin
  browser tools record an ordered ActionPlan
  mutating requests are captured, never forwarded
        │
        ▼
deterministic invariant engine
        │
   fail ├────────► ledger failure + no production mutation
        │ pass
        ▼
stored rehearsal + action_plan_hash
        │
        ▼
trust gate
  L1 deny │ L2 approve │ L3 veto timer │ L4 auto
        │
        ▼
sidecar-owned production replayer
  exact action and commit-payload verification
        │
        ▼
verified outcome ──► ledger transition ──► dashboard
~~~

### Trust and model boundaries

GPT-5.6 is responsible for:

- Inferring intent-level steps from demonstrations.
- Identifying inputs and decision points.
- Resolving semantic anchors.
- Choosing actions in the twin.
- Producing explanations shown in the run timeline.

Deterministic code is responsible for:

- Canonicalization and hashes.
- Run state transitions.
- Trust scoring and promotion/demotion.
- Host, method, action, and payload checks.
- Approval and veto enforcement.
- Production replay.
- Outcome attribution.

The model supplies intelligence. The sidecar owns authority.

---

## 6. Core contracts

### 6.1 Playbook

~~~python
class Playbook(BaseModel):
    task: str
    goal: str
    inputs: list[InputSpec]
    preconditions: list[str]
    steps: list[PlaybookStep]
    decision_points: list[DecisionPoint]
    success_criteria: SuccessCriteria
    version: int

class PlaybookStep(BaseModel):
    intent: str
    action: Literal["navigate", "fill", "upload", "click", "commit"]
    anchor: Anchor
    value_template: str | None
    effect: Literal["observe", "prepare", "commit"]
~~~

Rules:

- At least two demonstrations are required.
- Each declared input must be used by a value template or decision point.
- A recorded mutating request requires at least one effect=commit step.
- A commit step cannot be downgraded to click during execution.
- Unknown action/effect combinations fail validation.
- Re-induction increments the version and resets the bucket to L1.

### 6.2 Run specification

~~~python
class RunSpec(BaseModel):
    run_id: str
    bucket_id: int
    playbook_version: int
    playbook_digest: str
    inputs: dict[str, JSONValue]
    input_digest: str
    target_base_url: str
    allowed_hosts: list[str]
    tool_versions: dict[str, str]
    toolset_digest: str
~~~

Canonical JSON uses sorted keys, UTF-8, and compact separators. The sidecar calculates every digest. The approved target URL, allowed hosts, current reviewed playbook digest, and tool versions are copied from the bucket/playbook configuration; a run caller supplies only the bucket name and inputs.

### 6.3 Recorded action

~~~python
class RecordedAction(BaseModel):
    ordinal: int
    tool_name: Literal["navigate", "fill", "upload", "click", "commit"]
    arguments: dict[str, JSONValue]
    arguments_digest: str
    effect: Literal["observe", "prepare", "commit"]
~~~

The action_plan_hash is the SHA-256 digest of the canonical ordered action list. Production consumes actions in ordinal order exactly once.

### 6.4 Run lifecycle

~~~text
created
  └─► rehearsing
        ├─► rehearsal_failed
        └─► rehearsed
              ├─► approval_pending
              ├─► veto_pending
              ├─► authorized
              └─► denied
approval_pending
  ├─► authorized
  └─► denied
veto_pending
  ├─► authorized
  └─► vetoed
authorized
  ├─► executing
  └─► expired
executing
  ├─► succeeded
  └─► failed
~~~

rehearsal_failed, denied, vetoed, expired, succeeded, and failed are terminal. Every transition is validated in one service. Invalid or repeated transitions return a conflict and do not mutate state.

### 6.5 Rehearsal checks

Every rehearsal returns these named checks:

1. action_plan_matches_playbook
2. commit_payload_matches_inputs
3. no_undeclared_hosts
4. success_criteria_met
5. no_improvised_response_on_critical_path
6. no_network_egress_for_mutations

A rehearsal passes only when all checks pass.

---

## 7. Persistence model

The schema may evolve during implementation, but these records and uniqueness constraints are required:

~~~sql
CREATE TABLE buckets (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('playbook', 'tool')),
  risk_class TEXT NOT NULL CHECK(risk_class IN ('low','medium','high','critical')),
  level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 4),
  origin TEXT NOT NULL,
  playbook_version INTEGER NOT NULL,
  target_base_url TEXT NOT NULL,
  allowed_hosts_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_activity_at REAL NOT NULL,
  last_decay_at REAL
);

CREATE TABLE playbooks (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  version INTEGER NOT NULL,
  artifact_ref TEXT NOT NULL,
  digest TEXT NOT NULL,
  reviewed_at REAL NOT NULL,
  UNIQUE(bucket_id, version),
  UNIQUE(bucket_id, digest)
);

CREATE TABLE demonstrations (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER REFERENCES buckets(id),
  artifact_ref TEXT NOT NULL,
  artifact_digest TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('training','heldout')),
  created_at REAL NOT NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  playbook_version INTEGER NOT NULL,
  playbook_digest TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  target_base_url TEXT NOT NULL,
  allowed_hosts_json TEXT NOT NULL,
  tool_versions_json TEXT NOT NULL,
  toolset_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'created','rehearsing','rehearsal_failed','rehearsed',
    'approval_pending','veto_pending','authorized','denied',
    'executing','succeeded','failed','vetoed','expired'
  )),
  action_plan_hash TEXT,
  sdk_state_ref TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE run_transitions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE rehearsals (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  passed INTEGER NOT NULL,
  checks_json TEXT NOT NULL,
  trace_ref TEXT NOT NULL,
  completed_at REAL NOT NULL
);

CREATE TABLE run_actions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  ordinal INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  arguments_digest TEXT NOT NULL,
  effect TEXT NOT NULL CHECK(effect IN ('observe','prepare','commit')),
  status TEXT NOT NULL CHECK(status IN ('planned','executing','succeeded','failed')),
  UNIQUE(run_id, ordinal)
);

CREATE TABLE verdicts (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  decision TEXT NOT NULL CHECK(decision IN ('deny','approval','veto','authorize')),
  decided_by TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE vetoes (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  deadline REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','authorized','vetoed','expired')),
  resolved_at REAL
);

CREATE TABLE events (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  run_id TEXT REFERENCES runs(id),
  type TEXT NOT NULL,
  weight REAL NOT NULL,
  severity TEXT CHECK(severity IS NULL OR severity IN ('minor','major','critical')),
  evidence_key TEXT UNIQUE NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
~~~

Connections enable foreign_keys, WAL, and a busy timeout. State changes plus their transition audit row and ledger event occur in one BEGIN IMMEDIATE transaction using a compare-and-swap state update. Concurrent attempts from the same expected state have exactly one winner. Repository tests use file-backed temporary databases; per-operation connections must not use plain :memory: databases.

---

## 8. File structure

~~~text
apprentice/
  config.py
  canonical.py
  models.py
  ledger/
    db.py
    repository.py
    scoring.py
    transitions.py
  sidecar/
    app.py
    run_service.py
    approval_service.py
    console.py
    templates/
      dashboard.html
      run.html
  recorder/
    session.py
    redact.py
    artifacts.py
  induction/
    induce.py
    normalize.py
    shadow.py
  twin/
    builder.py
    snapshot.py
    invariants.py
    responses.py
  executor/
    agent.py
    twin_tools.py
    replay.py
    sdk_state.py
demo_portal/
  app.py
trust_policy.yaml
fixtures/
  expense/
    training_1/
    training_2/
    heldout/
    changed_site/
tests/
  e2e/
scripts/
  make_fixtures.py
  run_demo.py
docs/
  codex-build-log.md
~~~

---

## 9. Trust policy for the demo

All values live in trust_policy.yaml.

~~~yaml
half_life_days: 30
decay_idle_days: 21
severity_demotion:
  major: 1
  critical: 2
signal_weights:
  rubber_stamp: 0.3
  default: 1.0
  engaged: 1.2
twin_pass_weight: 0.25
veto_seconds: 5
risk:
  low:      {sample_mult: 1.0, score_bonus: 0.00, max_level: 4}
  medium:   {sample_mult: 1.0, score_bonus: 0.00, max_level: 4}
  high:     {sample_mult: 2.0, score_bonus: 0.03, max_level: 4}
  critical: {sample_mult: 2.0, score_bonus: 0.03, max_level: 3}
levels:
  1: {min_training_demonstrations: 2}
  2: {min_unique_shadow_passes: 1, min_score: 0.85}
  3: {min_approved_successes: 2, min_score: 0.90, max_vetoes_recent: 1}
  4: {min_l3_successes: 5, min_score: 0.95}
~~~

These are explicitly demo thresholds. The dashboard labels the active policy as Demo Policy.

---

## 10. Implementation tasks

### Task 1: Scaffold and observable expense portal

**Objective:** Establish a runnable project and a target whose side effects can be measured conclusively.

**Files:**

- Create pyproject.toml
- Create trust_policy.yaml
- Create apprentice/__init__.py
- Create apprentice/config.py
- Create demo_portal/__init__.py
- Create demo_portal/app.py
- Create tests/test_config.py
- Create tests/test_portal.py
- Create tests/conftest.py

**Required interfaces:**

- load_policy(path) -> Policy
- model_name() -> str, default gpt-5.6
- build_portal(failure_mode: str = "none") -> FastAPI
- Portal routes:
  - GET /login and POST /login
- GET /expense
- POST /expense
- GET /api/policies
  - GET /api/debug/mutations
  - POST /api/debug/reset

**Portal behavior:**

- Recording and normal demos begin on /expense after login.
- The form exposes accessible merchant, amount, receipt, and justification controls.
- GET /api/policies returns a machine-readable max_without_justification threshold of 1000.
- Amounts over 1000 require nonblank justification before mutation.
- A successful POST /expense increments mutation_count exactly once.
- The confirmation includes Expense ID.
- failure_mode=rename_amount changes the accessible amount-field name.
- failure_mode=commit_schema changes the commit field expected by the server.
- GET routes never increment mutation_count.

**Steps:**

- [ ] Write pyproject.toml with the declared runtime/dev dependencies, including python-multipart and Ruff.
- [ ] Write failing config tests for policy loading and APPRENTICE_MODEL.
- [ ] Implement frozen policy models and explicit validation, including unknown nested fields and unsafe YAML rejection.
- [ ] Write failing portal tests for form accessibility, upload, confirmation, and mutation_count.
- [ ] Test the high-value justification branch and prove invalid submissions do not mutate.
- [ ] Test that portal state/reset is isolated per app and concurrent valid posts receive unique IDs with an exact final count.
- [ ] Write failing tests for both meaningful failure modes.
- [ ] Implement the minimal portal.
- [ ] Add free-port and threaded-server fixtures with deterministic cleanup.
- [ ] Add a placeholder E2E smoke test that starts the portal and proves GET /expense has no side effect.
- [ ] Run uv sync and uv run playwright install chromium.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- The portal is runnable locally.
- Normal submit changes mutation_count from 0 to 1.
- Reading or rendering the form leaves it at 0.
- Both injected failures are visible to later twin tests.

---

### Task 2: Safety kernel — repository, run state, and non-replayable evidence

**Objective:** Make the sidecar the only authority for digests, state transitions, verdicts, and ledger evidence.

**Files:**

- Create apprentice/canonical.py
- Create apprentice/models.py
- Create apprentice/ledger/db.py
- Create apprentice/ledger/repository.py
- Create apprentice/ledger/scoring.py
- Create apprentice/ledger/transitions.py
- Create apprentice/sidecar/app.py
- Create apprentice/sidecar/run_service.py
- Create tests/ledger/test_repository.py
- Create tests/ledger/test_transitions.py
- Create tests/sidecar/test_runs.py

**Required interfaces:**

- canonical_json(value) -> bytes
- digest(value) -> str
- Repository(db_path)
- Repository.create_bucket(...) — defaults to the policy-safe level; test setup may use an explicit private fixture helper
- Repository.register_reviewed_playbook(...)
- Repository.add_demonstration(..., role)
- RunService.create_run(bucket_name, inputs) -> Run — copies target/hosts/playbook/tool snapshots from approved capability state
- Named RunService lifecycle methods for starting/completing rehearsal, requesting/resolving approval, starting execution, and recording the verified outcome
- Private repository compare-and-swap transition primitive; no generic transition API
- Typed evidence methods whose type, severity, weight, bucket/run, and evidence key are derived by the service
- promotion_eligible(...)
- record_verified_outcome(...)

**Rules:**

- create_run reads the current playbook version and computes input_digest itself.
- create_run copies the reviewed playbook digest, approved normalized target, allowed hosts, and toolset versions into the immutable run snapshot.
- Bucket creation never accepts an arbitrary initial trust level from an HTTP caller.
- Direct level mutation is not exposed as an API.
- Evidence insertion is idempotent by evidence_key.
- Reusing an evidence key with different content is a conflict, not a silent dedupe.
- Unknown event types are rejected.
- Only named service methods create shadow, rehearsal, approval, veto, outcome, and audit events.
- Major and critical demotions happen transactionally with the failure event and run-state audit row.
- State changes use SQL compare-and-swap; concurrent transitions from one expected state have exactly one winner.
- The repository uses a fresh managed connection per transaction.
- API request models forbid extra fields, including caller-supplied IDs, levels, states, digests, weights, severities, rehearsal claims, and evidence keys.
- Target URLs reject credentials, query/fragment components, and disallowed schemes; callers cannot expand allowed hosts.

**Steps:**

- [ ] Write failing canonicalization tests showing key order does not change a digest.
- [ ] Write the SQLite schema and connection setup.
- [ ] Write failing tests for legal and illegal run-state transitions.
- [ ] Implement named RunService lifecycle methods as the sole transition authority.
- [ ] Write sequential and concurrent tests showing identical evidence dedupes once and conflicting evidence is rejected.
- [ ] Write tests for major and critical immediate demotion.
- [ ] Write a test proving an HTTP caller cannot supply level, weight, or a trusted rehearsal flag.
- [ ] Write a barrier-synchronized test proving two transitions from the same expected state produce one success and one conflict.
- [ ] Write a forced-failure test proving transition, event, and demotion writes roll back together.
- [ ] Write concurrent FastAPI/SQLite tests that complete without database-is-locked failures.
- [ ] Add typed FastAPI error responses for unknown buckets, conflicts, and invalid transitions.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- No public API can manufacture trust evidence.
- No caller can mark its own run rehearsed.
- Duplicate traces/runs do not advance trust.
- Every state transition and event is auditable.

---

### Task 3: Recorder, sanitized artifacts, and reproducible fixtures

**Objective:** Capture useful post-login demonstrations while proving retained artifacts exclude secrets.

**Files:**

- Create apprentice/recorder/session.py
- Create apprentice/recorder/redact.py
- Create apprentice/recorder/artifacts.py
- Create tests/recorder/test_redact.py
- Create tests/recorder/test_session.py
- Create scripts/make_fixtures.py
- Generate fixtures/expense/training_1
- Generate fixtures/expense/training_2
- Generate fixtures/expense/heldout
- Generate fixtures/expense/changed_site

**Artifact contract:**

~~~json
{
  "task": "file expense",
  "allowed_hosts": ["127.0.0.1"],
  "steps": [],
  "http_entries": [],
  "response_templates": [],
  "artifact_digest": "..."
}
~~~

**Capture requirements:**

- Start from an already authenticated /expense page.
- Capture click, input, upload, submit, navigate, and relevant page-state excerpts.
- Anchors include role, accessible name, and CSS fallback.
- Keep screenshots for useful steps only.
- Apply denylist checks before screenshots or event persistence.
- Never retain password values or one-time-code values.
- Remove authorization, cookie, and set-cookie headers.
- Redact configured sensitive keys in query strings and bodies.
- Retain observed mutation response templates separately, with source=observed.
- Generate artifacts through one reproducible script.

**Tests:**

- Password input is omitted/redacted.
- autocomplete=one-time-code is omitted/redacted.
- A denylisted host produces no step, screenshot, or retained HTTP entry.
- Header and body sanitizer removes configured secrets.
- A sentinel scan across every retained artifact does not find hunter2 or 123456.
- Two training traces vary merchant/amount.
- The held-out trace is not used by induction.
- changed_site records the meaningful accessible-name or commit-schema change.

**Steps:**

- [ ] Write the redaction and artifact tests first.
- [ ] Implement capture with injected clock and output paths.
- [ ] Implement final sanitization before artifacts move into their retained location.
- [ ] Ensure temporary raw artifacts are cleaned on success and expected failure.
- [ ] Implement scripts/make_fixtures.py.
- [ ] Generate and inspect all four fixture sets.
- [ ] Run the sentinel scanner over fixtures.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- Fixture creation is one command.
- Final artifacts contain sufficient page/HTTP data for induction and twin replay.
- Secret sentinels are absent from every retained artifact.

---

### Task 4: GPT-5.6 induction and held-out shadow evaluation

**Objective:** Give GPT-5.6 a meaningful semantic role and prevent training traces from masquerading as independent evidence.

**Files:**

- Create apprentice/induction/normalize.py
- Create apprentice/induction/induce.py
- Create apprentice/induction/shadow.py
- Create tests/induction/test_normalize.py
- Create tests/induction/test_induce.py
- Create tests/induction/test_shadow.py
- Create fixtures/expense/expected_playbook.json
- Create fixtures/expense/eval_cases.json

**Induction design:**

- Define Pydantic output models for Playbook, InputSpec, PlaybookStep, DecisionPoint, Anchor, and SuccessCriteria.
- Construct one Agents SDK induction agent with model=model_name() and output_type=PlaybookDraft.
- Inject a model runner in tests; fake runs return typed fixtures.
- Deterministically detect values that vary across aligned demonstrations and include that evidence in the prompt.
- Require the model to cite which demonstrations support each decision point.
- Validate the result after the model returns it.
- Store a human-reviewed playbook version before creating the L1 bucket.

**Shadow design:**

- Use only held-out artifacts for qualifying shadow evidence.
- Ask GPT-5.6 for ProposedAction objects against recorded page states.
- Compare action, semantic anchor, rendered value, branch decision, and final success criterion.
- Use the evidence key shadow:{playbook_version}:{heldout_artifact_digest}.
- Repeating the same held-out trace returns the existing event and does not increase counts.

**Tests:**

- Fewer than two training demonstrations is rejected.
- Varying merchant and amount become inputs.
- The amount-over-policy branch becomes a decision point.
- A missing commit step fails playbook validation.
- A commit step mislabeled as prepare fails validation.
- Fake GPT output round-trips through the Pydantic schema.
- The held-out happy path passes.
- Wrong amount or wrong branch fails.
- Replaying the held-out trace twice produces one qualifying event.

**Steps:**

- [ ] Write the Pydantic contracts and validation tests.
- [ ] Implement normalization and deterministic input evidence.
- [ ] Implement the injectable GPT-5.6 induction agent.
- [ ] Save and review the expected playbook fixture.
- [ ] Implement held-out shadow scoring.
- [ ] Connect one qualifying shadow result to the sidecar service.
- [ ] Run one marked live smoke test and inspect the Agents SDK trace.
- [ ] Keep live tests excluded from normal pytest.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- The runtime genuinely uses GPT-5.6 for induction.
- A held-out run, not a training replay, creates the L2 promotion evidence.
- Prompt/model changes can be checked against the local eval fixtures.

---

### Task 5: Conservative twin and exact action-plan recording

**Objective:** Execute the agent against a current read-only snapshot, capture the exact intended actions and mutation, and fail closed on uncertainty.

**Files:**

- Create apprentice/twin/snapshot.py
- Create apprentice/twin/builder.py
- Create apprentice/twin/responses.py
- Create apprentice/twin/invariants.py
- Create apprentice/executor/agent.py
- Create apprentice/executor/twin_tools.py
- Create tests/twin/test_snapshot.py
- Create tests/twin/test_builder.py
- Create tests/twin/test_invariants.py
- Create tests/executor/test_twin_agent.py

**Snapshot strategy:**

- Immediately before rehearsal, perform a read-only capture of the current target GET pages required by the playbook.
- Merge those current safe reads over the recorded fixture reads.
- Record which hosts and URLs were observed.
- The claim is zero production side effects on rehearsal failure, not zero read-only production contact.

**Routing rules:**

- GET/HEAD matches exact method, host, normalized URL, and query.
- Preserve relevant status, response headers, encoding, and body.
- POST/PUT/PATCH/DELETE are always captured and never continued.
- A captured mutation receives a response only from a compatible observed response template.
- Unknown critical response templates fail; there is no LLM fallback.
- Any unseen host or unmatched request is recorded and aborted.

**Twin agent:**

- Use GPT-5.6 through the Agents SDK.
- Expose navigate, read_page, fill, upload, click, and commit function tools.
- Each tool appends one canonical RecordedAction.
- commit is distinct from click and is the only tool allowed to trigger the recorded mutation.
- Tool wrappers enforce the next playbook effect class.
- The model never receives a production browser.

**Invariant details:**

- Parse JSON, form-encoded, and multipart payloads structurally.
- Match input names to payload fields, not substrings.
- For files compare declared field, filename, and content hash.
- Ensure visited_hosts is a subset of observed/allowed hosts.
- Ensure every recorded action is compatible with the playbook.
- Ensure success criteria are satisfied against structured page state.
- Mark every response as observed, current_snapshot, or improvised.
- Any improvised critical-path response fails.

**Tests:**

- A normal expense rehearsal passes.
- A wrong amount fails commit_payload_matches_inputs.
- rename_amount fails before mutation.
- commit_schema fails payload validation.
- A request to an unseen host fails.
- A trap HTTP server receives zero twin mutations.
- Unknown mutation response template fails rather than inventing success.
- The ordered action plan and action_plan_hash are stable.

**Steps:**

- [ ] Implement and test the read-only snapshot merger.
- [ ] Implement exact request matching and response decoding.
- [ ] Implement mutation capture and observed-template responses.
- [ ] Implement canonical action recording.
- [ ] Implement all six named rehearsal checks.
- [ ] Connect the GPT-5.6 twin agent with an injectable fake runner.
- [ ] Store the passing rehearsal, actions, trace reference, and plan hash in one transaction.
- [ ] Inspect one live Agents SDK trace before prompt tuning.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- GPT-5.6 completes the normal task inside the twin.
- Mutations provably do not escape.
- The changed site fails before any production mutation.
- A pass produces a stored, immutable action plan.

---

### Task 6: Persistent approval/veto and exact production replay

**Objective:** Turn a passing rehearsal into a durable approval interruption and execute only the rehearsed plan.

**Files:**

- Create apprentice/sidecar/approval_service.py
- Create apprentice/executor/sdk_state.py
- Create apprentice/executor/replay.py
- Modify apprentice/executor/agent.py
- Modify apprentice/sidecar/app.py
- Create tests/sidecar/test_approvals.py
- Create tests/executor/test_sdk_state.py
- Create tests/executor/test_replay.py

**Approval tool:**

~~~python
@function_tool(needs_approval=True)
async def activate_rehearsed_plan(run_id: str) -> str:
    ...
~~~

The tool accepts only run_id. It re-reads the run, rehearsal, verdict, and plan hash from the sidecar. The model cannot supply or alter the production actions.

**Approval lifecycle:**

- When the SDK returns an interruption, finalize the twin checks first.
- Failed checks reject the interruption and transition to rehearsal_failed.
- L1 rejects activation.
- L2 stores approval_pending until the user approves/rejects.
- L3 stores veto_pending with a deadline; expiry authorizes unless canceled.
- L4 authorizes immediately.
- Critical risk never auto-authorizes beyond the L3 veto behavior.
- Store the SDK resumable state through an isolated SDKStateStore adapter.
- Add a serialization round-trip test against the installed SDK before relying on delayed resume.
- Resolving an approval resumes the same SDK run state.

**Production replayer:**

- The sidecar owns ProductionReplayer; the model receives no raw Playwright Page.
- Load actions only from run_actions ordered by ordinal.
- Recompute and verify action_plan_hash before starting.
- Consume every action exactly once.
- Allow navigation only to allowed hosts.
- Resolve anchors against the live accessibility tree.
- Intercept the real mutating request before forwarding it.
- Compare method, normalized URL, structured payload, and file hashes with the rehearsed mutation.
- If they match and the run is authorized, forward exactly once.
- Any mismatch aborts before forwarding and records a major failure.
- Verify the final success criterion and production mutation_count.

**Persistence and concurrency:**

- Veto status and deadline live in SQLite.
- Expiry resolution is transactional and idempotent.
- Restarting app services preserves pending items.
- A canceled or expired item cannot be resolved twice.
- Tests use an injected clock.

**Tests:**

- A caller cannot execute a run in created or rehearsal_failed state.
- L2 pauses and resumes after explicit approval.
- L2 rejection never touches production.
- L3 creates one countdown for the plan, not one per browser step.
- L3 cancellation prevents execution.
- L3 expiry survives a new app instance using the same database.
- Repeated resolve calls execute at most once.
- A changed action argument aborts.
- A changed commit payload aborts before forwarding.
- A normal replay increments mutation_count exactly once.
- SDK approval state round-trips and resumes.

**Steps:**

- [ ] Write approval state-machine tests.
- [ ] Implement SQLite-backed approvals and vetoes.
- [ ] Spike and test the current Agents SDK state serialization API behind SDKStateStore.
- [ ] Add activate_rehearsed_plan with needs_approval=True.
- [ ] Implement exact deterministic replay.
- [ ] Add live-network mutation interception and payload comparison.
- [ ] Add restart, cancel, expiry, and exactly-once tests.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- There is one approval boundary per rehearsed plan.
- Pending approval/veto survives restart.
- Production cannot diverge from the rehearsal.
- A production mutation happens at most once.

---

### Task 7: Trust transitions and one polished dashboard

**Objective:** Make earned and revoked autonomy legible without building a large administration product.

**Files:**

- Finalize apprentice/ledger/scoring.py
- Finalize apprentice/ledger/transitions.py
- Create apprentice/sidecar/console.py
- Create apprentice/sidecar/templates/dashboard.html
- Create apprentice/sidecar/templates/run.html
- Create tests/ledger/test_scoring.py
- Create tests/sidecar/test_console.py

**Trust rules:**

- Score uses a decayed Beta-style success ratio with all thresholds from policy.
- Sample requirements count unique qualifying evidence only.
- Twin passes have positive weight 0.25.
- Twin failures count at full failure weight.
- Rubber-stamp approvals have reduced positive weight.
- Vetoes/failures always count fully.
- Major failure immediately demotes one level.
- Critical failure immediately demotes two.
- Decay is applied lazily and at most once per idle interval.
- Promotion is eligible automatically but applied only after user confirmation.
- Re-induction resets the capability to L1.
- Two unique training demonstrations plus a reviewed playbook activate L1 atomically; the intermediate induced-but-unreviewed record is not executable.
- Effective level is the minimum of the playbook and any declared tool dependency.

**Dashboard:**

- One capability card per bucket:
  - name and task
  - L0–L4 ladder
  - risk
  - score
  - unique evidence summary
  - next promotion requirements
  - latest demotion cause
- One live run timeline:
  - read-only snapshot
  - GPT-5.6 twin actions
  - invariant checklist
  - approval/veto state
  - production replay
  - verified outcome
- Approval controls:
  - Approve
  - Reject with reason
  - Cancel veto
- Promote control shown only when eligible.
- SSE refreshes state without a SPA framework.

**Tests:**

- Five duplicates of one evidence key still count as one.
- High risk scales samples and score threshold.
- Critical caps at L3.
- Major and critical demotion are immediate.
- Decay does not repeatedly drain levels on every read.
- Dashboard renders the correct evidence and state.
- Approval buttons call only the approval service.
- The run page displays all named invariants.

**Steps:**

- [ ] Complete the scoring and transition tests.
- [ ] Implement signal weighting and lazy decay.
- [ ] Build the dashboard context as a pure service function.
- [ ] Render the dashboard and run templates.
- [ ] Add SSE updates using persisted state.
- [ ] Add minimal CSS polish and responsive layout.
- [ ] Manually inspect the rendered UI at desktop and narrow width.
- [ ] Checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- A judge can understand why a capability has its current level in seconds.
- Approval, veto, pass, failure, promotion, and demotion are visible in one coherent experience.

---

### Task 8: End-to-end proof, evals, demo, and submission assets

**Objective:** Prove the product claim automatically and package it clearly for judging.

**Files:**

- Create tests/e2e/test_demo_arc.py
- Create fixtures/expense/eval_cases.json
- Create scripts/run_demo.py
- Create README.md
- Create docs/codex-build-log.md
- Create docs/demo-script.md
- Create docs/limitations.md

**Automated demo arc:**

1. Load two training demonstrations.
2. Induce and validate Playbook v1 with a fake deterministic model response in CI.
3. Run one unique held-out shadow evaluation.
4. Confirm L2 eligibility and promote.
5. Create a new RunSpec with new merchant/amount.
6. Capture the current read-only snapshot.
7. Rehearse in the twin and store the action plan.
8. Confirm the production mutation counter is still zero.
9. Resolve the L2 approval and replay production.
10. Confirm the counter is exactly one and the run succeeds.
11. Complete a second approved success and promote to L3.
12. Start an L3 run, let the injected veto clock expire, and execute once.
13. Switch to a meaningful changed-site mode.
14. Rehearsal fails.
15. Production mutation counter remains unchanged.
16. Capability automatically demotes with a visible reason.

**Required adversarial assertions:**

- Forged rehearsal claims are rejected.
- Duplicate held-out evidence does not promote.
- Unknown hosts fail.
- Unknown critical responses fail.
- Wrong commit amount fails.
- Replay action mismatch fails.
- Canceled veto never executes.
- Restarted veto state remains pending/resolvable.
- Failed rehearsal creates zero production mutation.
- All retained artifacts pass the secret sentinel scan.

**Evaluation corpus:**

- Normal expense.
- Amount over 1000 requiring justification.
- Changed accessible label.
- Unexpected external host.
- Wrong amount in commit payload.

Each case contains expected playbook facts, expected action-plan facts, and expected invariant outcomes. Run the corpus after every prompt change.

**Live verification:**

- Mark API-backed tests live.
- Run the normal and high-amount cases with GPT-5.6.
- Inspect the Agents SDK traces for model calls, tools, guardrails/approval interruption, and final outcome.
- Save trace identifiers or screenshots for the demo notes without committing secrets.
- Confirm the local deterministic eval results remain green.

**Submission experience:**

- README opens with the problem, one-sentence solution, architecture image, and three-command local run.
- The demo script uses the actual dashboard and a headed browser.
- The video prioritizes:
  1. teach
  2. held-out proof
  3. twin rehearsal
  4. approval and exact production replay
  5. changed-site failure with unchanged mutation count
  6. demotion
- docs/codex-build-log.md truthfully describes how Codex was used for planning, implementation, tests, adversarial review, and UI iteration.
- docs/limitations.md states the single-domain, post-login, local-machine boundary and the deferred self-authoring work.
- Re-check the challenge rules and submission requirements after July 13.

**Steps:**

- [ ] Write the complete E2E test before final UI polish.
- [ ] Make the E2E test deterministic and green.
- [ ] Run the five-case local eval corpus.
- [ ] Run the marked GPT-5.6 live checks and inspect traces.
- [ ] Write and rehearse scripts/run_demo.py.
- [ ] Complete README and limitation disclosure.
- [ ] Record the final demo from a clean setup.
- [ ] Final checkpoint: uv run ruff check . and uv run pytest -q.

**Acceptance:**

- A clean machine can follow the README and run the project.
- The automated test proves the central safety claim.
- The video shows a complete product rather than disconnected technical components.

---

## 11. Build-week schedule

| Day | Deliverable |
|---|---|
| 1 | Task 1 plus the first portal E2E smoke path |
| 2 | Task 2 safety kernel |
| 3 | Task 3 recorder and fixtures |
| 4 | Task 4 induction and held-out shadow |
| 5 | Task 5 twin and real GPT-5.6 rehearsal |
| 6 | Task 6 approvals and exact production replay |
| 7 | Task 7 dashboard and full E2E integration |
| 8 / buffer | Task 8 polish, live evals, demo recording, submission |

Do not postpone integration until the last task. At the end of every day, run the longest available vertical slice.

---

## 12. Cut order if schedule slips

Cut in this order:

1. Declarative self-tool stretch.
2. L4 UI and decay visualization.
3. High-risk sample multiplier UI.
4. SSE polish; retain manual refresh.
5. Live trace-grader integration; retain local evals and SDK traces.

Do not cut:

- Sidecar-owned rehearsal verification.
- Non-replayable evidence.
- No-egress mutation handling.
- Exact production replay.
- Persistent approval/veto.
- Failed-rehearsal production mutation assertion.
- Secret artifact scan.
- The changed-site demo.

---

## 13. Spec deltas for the Build Week implementation

This implementation plan intentionally narrows or clarifies the broader design spec:

- “Zero production contact” becomes “zero production side effects”; a fresh read-only snapshot is allowed.
- Arbitrary Python self-authoring is deferred.
- The critical path has no LLM mock responder.
- L4 audit scheduling is deferred.
- The recorder starts post-login and is not presented as a general credential-safe browser recorder.
- Two unique training demonstrations plus reviewed playbook content activate L1; this is the Build Week resolution of the design spec's L0/L1 ambiguity.
- The dashboard replaces five separate console views.
- The system is demonstrated on one domain and is not marketed as production-ready general browser automation.
- GPT-5.6 acts in the twin; production is deterministic exact-plan replay.

These deltas strengthen the truthfulness and testability of the submission without changing the central product thesis.

---

## 14. Final definition of done

The project is ready to submit only when:

- [ ] uv run ruff check . passes.
- [ ] uv run pytest -q passes.
- [ ] The live GPT-5.6 induction and rehearsal smoke tests pass.
- [ ] The normal demo increments production mutation_count exactly once.
- [ ] The changed-site demo increments it zero times.
- [ ] A forged rehearsal flag/token cannot start production.
- [ ] Duplicate evidence cannot advance trust.
- [ ] A veto survives restart and executes at most once.
- [ ] Secret sentinels are absent from retained artifacts.
- [ ] The dashboard explains each promotion/demotion from concrete evidence.
- [ ] The README works from a clean environment.
- [ ] The demo has been rehearsed end-to-end at least twice.
- [ ] The submission accurately states limitations and deferred work.

---

## 15. Official references to verify during implementation

- OpenAI Build Week: https://openai.com/build-week/
- Challenge page and current judging criteria: https://openai.devpost.com/
- Agents SDK quickstart: https://developers.openai.com/api/docs/guides/agents/quickstart
- Guardrails and human review: https://developers.openai.com/api/docs/guides/agents/guardrails-approvals
- Agent evaluation guidance: https://developers.openai.com/api/docs/guides/agent-evals

When SDK details differ from this plan, follow the current official documentation, keep the safety invariants unchanged, and record the small implementation decision in docs/codex-build-log.md.
