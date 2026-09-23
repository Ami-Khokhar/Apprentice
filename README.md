# Apprentice

[![CI](https://github.com/Ami-Khokhar/Apprentice/actions/workflows/ci.yml/badge.svg)](https://github.com/Ami-Khokhar/Apprentice/actions/workflows/ci.yml)

> A minimalist AI dojo for practicing professional judgment under pressure.

Apprentice creates realistic, role-specific situations for the work someone does
today—or wants to do next. The learner studies the evidence, explains a decision
in their own words, experiences deterministic consequences, and receives an
evidence-grounded debrief.

It is deliberate practice for decisions, not a course about recall.

## Two ways to run

- **Local** — sign in with the Codex CLI, single user, data in a local SQLite file.
- **Hosted** — bring your own OpenAI API key, per-visitor history, deployed with Docker or Render.

## Why Apprentice exists

Professional judgment is often learned after a costly mistake or by watching an
experienced colleague handle a rare event. Static courses can teach concepts,
but they struggle to recreate ambiguity, incomplete evidence, time pressure, and
the trade-offs of real work.

Apprentice gives learners a safe place to rehearse those moments before they
happen in real life. It is designed for:

- people preparing to enter a profession;
- career changers learning an unfamiliar role;
- practitioners taking on greater responsibility; and
- experienced professionals who want to rehearse rare, consequential situations.

Apprentice runs as a local, single-user experience for individual practice, or
as a hosted, bring-your-own-key deployment where each visitor keeps their own
history.

## The practice loop

1. Describe a professional field, work context, and desired difficulty.
2. Apprentice generates a new situation calibrated to that learner.
3. The facts, timeline, evidence, metrics, actions, consequences, recovery
   conditions, and assessment rubric are validated and frozen.
4. The learner reviews the briefing and asks factual clarification questions.
5. The learner explains what they would do and why in free text.
6. The facilitator recognizes the proposed plan and maps at most one immediate
   step to the deterministic world.
7. The simulation applies that action, advances time, and reveals its observable
   consequences.
8. The learner continues until recovery, terminal escalation, or an intentional
   early stop.
9. Apprentice produces a scored debrief grounded in the recorded decisions and
   frozen evidence.

The learner can then revisit the problem, proposed solution, executed actions,
outcome, and debrief in **My practice**.

## A portfolio of judgment

Apprentice turns completed and in-progress sessions into a local Judgment
Profile. It separates three things that are often blurred together:

1. **What the learner proposed** — their original response and recognized plan.
2. **What the simulation executed** — the immediate action mapped into the
   deterministic world.
3. **What happened** — the resulting state, evidence, score, and debrief.

The profile reports problems as:

- **Encountered** — every generated practice situation;
- **Resolved** — situations that reached deterministic recovery;
- **Reviewed** — terminal or intentionally ended situations ready for reflection;
  and
- **In progress** — active situations that can still be continued.

Every case remains traceable to the learner's recorded words and observable
simulation evidence. Apprentice presents these as simulated practice records,
not verified workplace experience or claims of mastery.

## Why the simulation is inspectable

Most AI role-play lets one model invent the scenario, change the world, and judge
the result. Apprentice separates those responsibilities.

### Generative breadth

GPT-5.6 Terra creates situations for different professions, roles, and experience
levels instead of selecting from a fixed catalog.

### Deterministic consequences

After a generated world passes validation, the model cannot change its state.
`GeneratedScenarioRuntime` owns time, metric effects, revealed artifacts,
completed actions, recovery, and terminal escalation.

### Evidence-bounded assessment

Facilitation and debriefs may cite only canonical actions, metrics, artifacts,
and events from the frozen world. Invalid references fail closed before a state
transition is applied.

### Multiple defensible paths

The facilitator evaluates real-world plausibility rather than conformity to one
hidden answer sequence. A sound proposal can be recognized even when it differs
from the generated action order or has no exact transition equivalent.

### Scenario novelty

Apprentice compares a new situation with recent sessions in the same professional
field. It checks both a structural fingerprint and semantic similarity across the
core challenge, failure mechanism, and decision trade-off. Near-duplicates are
regenerated up to a bounded limit.

## Product experience

The interface keeps one decision in focus through five calm, server-rendered
surfaces:

1. **Entry** — choose the field, work context, and difficulty.
2. **Briefing** — understand what happened, the role, constraints, and first
   decision.
3. **Practice** — ask factual clarifications, propose a response, and observe the
   situation evolve.
4. **Debrief** — review the score, strengths, missed evidence, risky assumptions,
   and a stronger path.
5. **My practice** — browse the Judgment Profile and open the complete record for
   any case.

Long-running model operations use an accessible loading layer that preserves the
learner's orientation without crowding the underlying screen.

## Model boundary

GPT-5.6 Terra has four bounded responsibilities:

1. generate a complete `GeneratedScenarioSpec`;
2. interpret a learner response as one action, a defensible unmodelled decision,
   or a request for clarification;
3. answer factual clarification questions from currently observable evidence;
   and
4. produce the final debrief from the frozen world and learner transcript.

Terra does **not** directly mutate the simulation.

In local mode, each model operation invokes `codex exec` with:

- the explicit `gpt-5.6-terra` model;
- ephemeral execution;
- a read-only sandbox and isolated temporary directory;
- a strict JSON output schema;
- learner content supplied through standard input; and
- a small environment allowlist required to locate and authenticate Codex.

Shell access, browser use, apps, plugins, multi-agent execution, image generation,
and web search are disabled inside the model subprocess.

In hosted mode, the same operations call the OpenAI Responses API with each
visitor's own API key and the same structured output contracts.

## Architecture

```text
Learner
  │
  ▼
FastAPI + Jinja interface
  │
  ▼
PracticeService ───────────────► SQLite sessions + Judgment Profile
  │
  ├──► Codex CLI / GPT-5.6 Terra
  │       ├── scenario generation
  │       ├── decision interpretation
  │       ├── factual clarification
  │       └── final debrief
  │
  └──► GeneratedScenarioRuntime
          └── deterministic state transitions
```

Key modules:

- `apprentice/incident/generated.py` — scenario validation, novelty detection,
  deterministic execution, and restoration.
- `apprentice/practice/` — typed contracts, Codex runner, orchestration,
  persistence, debriefing, and portfolio projection.
- `apprentice/sidecar/` — FastAPI routes, Jinja templates, styles, and loading
  behavior.
- `apprentice/observability.py` — privacy-aware local and optional external
  tracing.
- `apprentice/database.py` — file-backed SQLite lifecycle and transactions.
- `tests/` — runtime, model-boundary, service, route, template, profile, and
  observability coverage.

## Run Apprentice

### Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- a locally authenticated Codex account with access to GPT-5.6 Terra

The current application has been verified on macOS.

### Install

```bash
git clone https://github.com/Ami-Khokhar/Apprentice.git
cd Apprentice
uv sync --locked
codex login
```

### Start

```bash
uv run uvicorn apprentice.sidecar.app:build_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Run `codex login status` if authentication needs to be checked. Apprentice uses
the local ChatGPT/Codex login and does not require `OPENAI_API_KEY`.

Sessions and the Judgment Profile are stored locally in `apprentice.db`. Existing
sessions automatically appear in **My practice**; no seed data or separate
application service is required.

> [!WARNING]
> Local mode is a single-user application. Keep it bound to `127.0.0.1`. It is
> not hardened for LAN or internet exposure, reverse-proxy deployment, shared
> machines, or untrusted users. For a shared deployment, use hosted mode and
> read its limitations below.

### Run the hosted mode

Hosted mode turns on with `APPRENTICE_MULTI_USER=true`. Each visitor supplies
their own OpenAI API key in the browser, and the model runner calls the OpenAI
Responses API with structured output. `APPRENTICE_PRACTICE_MODEL` is required;
startup fails without it.

Build and run the container:

```bash
docker build -t apprentice .
docker run -p 8000:8000 -e APPRENTICE_PRACTICE_MODEL=<model> apprentice
```

On Render, `render.yaml` is a blueprint for a free web service. Set
`APPRENTICE_PRACTICE_MODEL` and `APPRENTICE_PUBLIC_HOST` in the dashboard.
Render free web services have an ephemeral filesystem, so the SQLite file is
lost on redeploy or restart.

| Variable | Purpose |
|---|---|
| `APPRENTICE_MULTI_USER` | Turn on hosted mode. The Dockerfile sets it to `true`. |
| `APPRENTICE_PRACTICE_MODEL` | Required in hosted mode; the model used for every operation. |
| `APPRENTICE_PUBLIC_HOST` | Optional; when set, enforce the Host header allowlist for that hostname. |
| `APPRENTICE_DB_PATH` | Database file path (default `apprentice.db`; the Dockerfile sets `/home/user/app/apprentice.db`). |

Observer is a local-only tool and must not be enabled in hosted mode.

## Data and privacy

Apprentice is local-first, but model-assisted practice is not fully local.
For scenario generation, facilitation, clarification, and debriefing, Apprentice
invokes the locally authenticated Codex CLI. Relevant data—including the
learner's professional field and work context, free-text responses, scenario
state and evidence, and the model prompts that contain those values—is sent to
OpenAI through that Codex session. The OpenAI account and product terms attached
to the local Codex login govern that processing.

In hosted mode, each visitor supplies their own OpenAI API key in the browser.
The page keeps the key in memory only, not in browser storage, and adds it as a
hidden form field on each practice submit. JSON API clients send it in the
`X-Apprentice-API-Key` header. The server holds the key in a request-scoped
context for that turn only and never stores it in the database. Each browser
gets a random `apprentice_visitor` cookie that lasts 30 days and is HttpOnly.
Sessions and the Judgment Profile are scoped to that visitor id, so losing the
cookie means losing access to that history. Learner content goes to OpenAI under
the visitor's own API key.

On the learner's machine:

- profile information, sessions, responses, scenario state, outcomes, and
  debriefs are stored in `apprentice.db`;
- SQLite storage is not encrypted by Apprentice, so operating-system users or
  backups with access to the file may be able to read it; and
- optional Observer traces are stored separately in
  `.apprentice/traces.jsonl`, or at the path configured by
  `APPRENTICE_TRACE_PATH`.

Observer is a local inspection surface. It does not create a separate external
data flow by itself, but content tracing can preserve full prompts, learner text,
state, and outputs in the trace file. Langfuse is a separate, optional external
data flow: when enabled, trace records are sent to the configured Langfuse
service. With `APPRENTICE_TRACE_CONTENT=false`, the integration sends the
metadata-only representation; with it set to `true`, it sends full trace
content. Review the configured Langfuse provider's retention and privacy terms
before enabling it.

To delete local Apprentice data, stop the application and remove:

```text
apprentice.db
apprentice.db-shm
apprentice.db-wal
.apprentice/
```

If the database path was customized through the application factory, or
`APPRENTICE_TRACE_PATH` points elsewhere, delete those configured files instead.
This removes local copies only. It does not delete data already processed or
retained by OpenAI, Langfuse, operating-system backups, or another configured
service; use the relevant provider's controls for those copies.

## Private Observer

Observer is an optional, owner-only local dashboard for inspecting observable
model inputs, structured outputs, retries, validation results, selected actions,
world consequences, metric changes, and debrief scores. It is disabled by
default and is not linked from the learner interface.

```bash
export APPRENTICE_OBSERVER_ENABLED=true
export APPRENTICE_OBSERVER_TOKEN="$(openssl rand -hex 32)"
export APPRENTICE_TRACE_RETENTION_DAYS=30

uv run uvicorn apprentice.sidecar.app:build_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

Open `http://127.0.0.1:8000/observer` and enter the token. The token can also be
passed in a URL fragment—never a query parameter:

```text
http://127.0.0.1:8000/observer#token=YOUR_TOKEN
```

The fragment is not sent to the server or included in Uvicorn access logs. The
local bootstrap exchanges it for an HttpOnly, SameSite=Strict cookie and removes
it from the address bar.

Local traces are stored in `.apprentice/traces.jsonl`. Configuration:

| Variable | Default | Purpose |
|---|---:|---|
| `APPRENTICE_TRACE_PATH` | `.apprentice/traces.jsonl` | Local trace location |
| `APPRENTICE_TRACE_RETENTION_DAYS` | `30` | Retention window |
| `APPRENTICE_TRACE_CONTENT_LIMIT_BYTES` | `256000` | Per-record size cap |
| `APPRENTICE_TRACE_CONTENT` | `false` | Store full prompts, learner text, state, and outputs |

Privacy defaults are conservative. Without content capture, inputs and outputs
are represented only by type and serialized size. Enable
`APPRENTICE_TRACE_CONTENT=true` only when the local machine is appropriate for
storing potentially private professional information.

Common secret and authentication fields are recursively redacted, but no
automatic redactor can recognize every sensitive business detail. Keep Observer
bound to `127.0.0.1`; it also rejects non-loopback clients and will not start
without an owner token.

Observer exposes structured inputs, outputs, and deterministic state changes—not
a model's private hidden chain of thought.

## Optional Langfuse tracing

Apprentice can send the same observable operation hierarchy to Langfuse. It is
off by default, and the base installation does not require the Langfuse SDK.

```bash
uv sync --extra observability --locked

export APPRENTICE_LANGFUSE_ENABLED=true
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Observer and Langfuse can run together. Full content is sent only when
`APPRENTICE_TRACE_CONTENT=true`; otherwise tracing retains the conservative
metadata-only representation.

## Development

The automated suite uses deterministic fake model responses and requires neither
network access nor model credentials.

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Routes

### Learner interface

- `GET /`
- `POST /practice`
- `GET /practice/{session_id}`
- `POST /practice/{session_id}/responses`
- `POST /practice/{session_id}/clarifications`
- `POST /practice/{session_id}/stop`
- `GET /practice/{session_id}/debrief`
- `GET /profile`
- `POST /profile`
- `GET /profile/cases/{session_id}`

### JSON API

- `POST /api/practice/sessions`
- `GET /api/practice/sessions/{session_id}`
- `POST /api/practice/sessions/{session_id}/responses`
- `POST /api/practice/sessions/{session_id}/clarifications`
- `POST /api/practice/sessions/{session_id}/stop`
- `GET /api/practice/sessions/{session_id}/debrief`
- `GET /api/profile`
- `PUT /api/profile`
- `GET /api/profile/cases/{session_id}`

### Owner interface

Available only when Observer is explicitly enabled:

- `GET /observer`
- `POST /api/observer/session`
- `GET /api/observer/traces`
- `GET /api/observer/traces/{session_id}`

## Current limitations

- The local mode is single-user.
- Real practice in local mode requires a locally authenticated Codex account with
  access to GPT-5.6 Terra. Hosted mode requires each visitor's own OpenAI API
  key.
- Generation and assessment latency depend on the Codex session in local mode and
  on the OpenAI API in hosted mode.
- Only one model-backed turn runs per learner at a time. A second concurrent
  submission is rejected with HTTP 429 instead of being queued.
- Hosted mode has no rate limiting, abuse controls, or accounts. A visitor's
  history is tied to one browser cookie.
- The example Render blueprint uses a free web service with an ephemeral
  filesystem, so hosted practice history does not survive a redeploy or restart.
- The Judgment Profile represents simulated practice, not verified professional
  experience.
- Novelty detection is deliberately bounded; after three rejected generations,
  Apprentice returns a safe error.
- Learning outcomes have not yet been evaluated in a formal learner study.

## License

Licensed under the [Apache License 2.0](LICENSE). See
[ASSET_PROVENANCE.md](ASSET_PROVENANCE.md) for the provenance of tracked design
assets.
