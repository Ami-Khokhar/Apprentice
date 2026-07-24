# Apprentice

> A minimalist AI dojo for practicing professional judgment under pressure.

- **OpenAI Build Week track:** Education
- **Built with:** Codex using GPT-5.6 Sol
- **Powered by:** GPT-5.6 Terra through the locally authenticated Codex CLI

Apprentice turns a learner's real or future profession into a new simulated
stress situation. The learner receives the timeline and evidence, explains what
they would do, experiences deterministic consequences, and finishes with an
evidence-grounded debrief.

## The problem

Professional judgment is usually learned after a costly mistake or by watching
an experienced colleague handle a rare event. Static courses can teach concepts,
but they struggle to recreate the ambiguity, time pressure, incomplete evidence,
and trade-offs of real work.

This affects people entering a field, changing careers, preparing for greater
responsibility, or practicing situations that are too dangerous or expensive to
rehearse in reality.

## Why the Education track

Apprentice treats professional judgment as a learnable skill. It is designed for
students preparing for work, career changers entering unfamiliar roles, and
professionals who want deliberate practice before a high-pressure situation
becomes real.

The educational loop is specific: contextual briefing, learner-authored
decision, observable consequence, evidence-grounded reflection, and an improved
decision path. This submission is a working prototype for individual practice;
it does not yet claim measured learning outcomes or institutional validation.

## The idea

Apprentice provides deliberate practice for decisions rather than recall.

1. Describe a professional field and, optionally, the work you do or want to do.
2. GPT-5.6 Terra invents a role-specific situation instead of selecting one from
   a fixed scenario catalog.
3. Apprentice validates and freezes the facts, causal timeline, evidence,
   metrics, available actions, consequences, recovery conditions, and rubric.
4. Explain your decision in your own words.
5. Terra interprets that intent as at most one currently available action.
6. A deterministic runtime—not the model—applies the action and advances time.
7. Continue until recovery or terminal escalation, then receive a grounded
   debrief with strengths, missed signals, risky assumptions, and a better path.

## Why this is different

Most AI role-play is an unconstrained conversation: the model creates the
problem, changes the world, and judges its own result. Apprentice separates those
responsibilities.

- **Generative breadth:** Terra can invent situations for different professions
  and work contexts.
- **Deterministic consequences:** after generation, only validated rules can
  change state.
- **Evidence-bounded coaching:** assessments and debriefs may cite only canonical
  actions, metrics, artifacts, and events.
- **Semantic novelty:** prior causal structures are compared, so cosmetic
  rewrites are rejected.
- **Calm interaction:** the Japanese-dojo-inspired interface keeps one decision
  in focus instead of turning the exercise into a dense dashboard.

The result combines the range of a language model with the inspectability of a
small simulation engine.

## A new situation every time

Each generation request includes a unique nonce that Terra must return exactly.
Apprentice compares the new scenario with recent situations for the same
normalized professional field using:

- a fingerprint of its failure mechanism, trade-off, evidence, action graph,
  effects, and escalation path; and
- similarity across its core challenge, failure mechanism, and decision
  trade-off.

Near-duplicates are rejected and regenerated up to three times. If novelty cannot
be established, Apprentice fails safely instead of presenting a repeated
exercise.

## Product experience

The learner moves through five focused screens:

1. **Entry** — describe the field and optional work context.
2. **Briefing** — understand how the situation developed, the current
   constraints, and the first decision.
3. **Practice** — respond in free text and see the situation evolve.
4. **Debrief** — review what was noticed, what was missed, and a stronger
   decision sequence.
5. **My practice** — revisit an evidence-backed portfolio of encountered,
   resolved, and reviewed problems, including the learner's original proposals,
   executed actions, and observable outcomes.

While Terra generates a situation or evaluates a decision, a branded loading
layer overlays only the main screen. The underlying content stays visible but
blurred, maintaining orientation without making the page feel crowded.

## How GPT-5.6 is used

GPT-5.6 Terra has three bounded responsibilities:

1. Generate a complete `GeneratedScenarioSpec` tailored to the learner.
2. Interpret a free-text response as one enabled action or request precise
   clarification.
3. Produce the final debrief from the frozen world and learner transcript.

Terra does **not** directly change simulation state. Pydantic validates every
structured response, Apprentice verifies all cited evidence, and the
`GeneratedScenarioRuntime` owns transitions, timing, metric effects, revealed
artifacts, success, and terminal escalation.

Each turn invokes `codex exec` with the explicit `gpt-5.6-terra` model,
ephemeral execution, a read-only sandbox, an isolated temporary directory, and a
JSON output schema. Shell, browser, apps, plugins, multi-agent use, image
generation, and web search are disabled. Learner content is sent through
standard input rather than process arguments, and the subprocess receives only a
small environment allowlist needed to locate and authenticate Codex.

## How Codex accelerated development

Codex using GPT-5.6 Sol was the primary development collaborator. The work was
split into bounded architecture, simulation, product interface, testing, and
review tasks, with subagents used for parallel implementation and independent
audits.

Codex accelerated:

- tracing the application from learner input through persistence and simulation;
- replacing finite authored situations with validated Terra-generated worlds;
- designing typed contracts for generation, facilitation, and debriefs;
- implementing semantic duplicate detection and bounded regeneration;
- integrating Terra through local ChatGPT authentication rather than an
  application API key;
- building the server-rendered dojo flow and branded loading state;
- testing failure cases such as invalid references, impossible graphs, repeated
  situations, unsafe subprocess inheritance, and restart restoration; and
- reducing the final repository to the smallest current-product dependency
  closure.

The dated commit history records these changes in progressive, reviewable
milestones.

## Key product and engineering decisions

| Decision | Human direction | Codex contribution |
|---|---|---|
| Generate rather than select scenarios | Every practice should feel new and match the learner's field. | Designed the structured generation contract, nonce check, semantic fingerprint, and retry path. |
| Keep the model out of world authority | Coaching should be flexible without allowing invented consequences. | Separated Terra's structured interpretation from deterministic state transitions and added evidence validation. |
| Use local Codex authentication | The learner flow should run through Codex rather than requiring an application API key. | Built and hardened the `codex exec` subprocess boundary. |
| Make waiting part of the product | Generation and judgment need visible feedback without replacing the page. | Implemented a branded, accessible overlay that blurs only the main surface. |
| Prefer calm focus over dashboard density | The simulation should not overwhelm or visually suffocate the learner. | Translated the Japanese dojo direction into a four-stage, server-rendered experience. |
| Keep the final project minimal | Only code that serves the current learning experience should remain. | Audited dependencies and removed unrelated runtime, demos, fixtures, routes, and packages. |

## Build Week development scope

Exploratory product research and a different prototype direction existed before
the submission period. That runtime is not part of the submitted product. The
current professional-judgment dojo and every retained application module were
built or meaningfully extended during OpenAI Build Week with Codex and GPT-5.6.

Work completed for this submission includes:

- the current education product concept and learner flow;
- GPT-5.6 Terra scenario generation, facilitation, and debriefing;
- generated-world validation and deterministic execution;
- semantic novelty rejection and retry behavior;
- durable generated-session restoration;
- the Japanese-dojo interface and loading treatment;
- the keyless, locally authenticated Codex runtime;
- focused security boundaries and regression tests; and
- removal of code unrelated to the submitted product.

Evidence is available in the repository's dated commit history and
[`docs/codex-dojo-development.md`](docs/codex-dojo-development.md).

Selected Build Week milestones:

- [validated Terra-generated worlds](https://github.com/Ami-Khokhar/Apprentice/commit/2febc8e);
- [local Codex authentication and structured execution](https://github.com/Ami-Khokhar/Apprentice/commit/8fcf679);
- [novel generation and decision facilitation](https://github.com/Ami-Khokhar/Apprentice/commit/471095c);
- [the minimalist dojo interface](https://github.com/Ami-Khokhar/Apprentice/commit/737be72); and
- [the final current-product reduction](https://github.com/Ami-Khokhar/Apprentice/commit/ed8a30b).

## Architecture

```text
Learner
  │
  ▼
FastAPI + Jinja dojo
  │
  ▼
PracticeService ───────────────► SQLite session history
  │
  ├──► Codex CLI / GPT-5.6 Terra
  │       ├── scenario specification
  │       ├── decision interpretation
  │       └── final debrief
  │
  └──► GeneratedScenarioRuntime
          └── deterministic state transition
```

The current application is intentionally small:

- `apprentice/incident/generated.py` — scenario validation, novelty detection,
  deterministic execution, and restoration.
- `apprentice/practice/` — typed contracts, Codex runner, orchestration, and
  session persistence, including the local judgment-profile projection.
- `apprentice/sidecar/` — FastAPI routes, dojo templates, CSS, and loading
  behavior.
- `apprentice/database.py` — file-backed SQLite lifecycle and transactions.
- `tests/` — generated-world, runner, service, route, and template coverage.

## Judge quickstart

### Requirements and supported platform

- A desktop environment that can run the Codex CLI (verified on macOS)
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- [Codex CLI](https://developers.openai.com/codex/cli/) with access to
  GPT-5.6 Terra

### Install and run

```bash
git clone https://github.com/Ami-Khokhar/Apprentice.git
cd Apprentice
codex login
uv sync --locked
uv run uvicorn apprentice.sidecar.app:build_app --factory --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Run `codex login status` if authentication needs to be checked. Apprentice uses
the local ChatGPT/Codex login and does not require `OPENAI_API_KEY`.

No sample dataset, seeded account, or separate application service is required
beyond the authenticated Codex CLI. Practice sessions are stored locally in
`apprentice.db`.

### Private Observer dashboard

The Observer is a separate, owner-only local dashboard for inspecting observable
model inputs, structured outputs, retries, validation results, selected actions,
world consequences, metric changes, and debrief scores. It is disabled by default
and is not linked from the learner interface.

```bash
export APPRENTICE_OBSERVER_ENABLED=true
export APPRENTICE_OBSERVER_TOKEN="$(openssl rand -hex 32)"
export APPRENTICE_TRACE_RETENTION_DAYS=30
# Sensitive: stores prompts, learner text, scenario state, and model outputs locally.
export APPRENTICE_TRACE_CONTENT=true
uv run uvicorn apprentice.sidecar.app:build_app --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/observer` and paste the token from
`APPRENTICE_OBSERVER_TOKEN`. You can also put it in a URL fragment—never a query
parameter—by opening `http://127.0.0.1:8000/observer#token=YOUR_TOKEN`. The fragment
is not sent to the server or included in Uvicorn access logs; the local bootstrap
exchanges it for an HttpOnly, SameSite=Strict cookie and immediately removes it
from the address bar.

Local traces are written to `.apprentice/traces.jsonl`. Change the location with
`APPRENTICE_TRACE_PATH` or the per-record cap with
`APPRENTICE_TRACE_CONTENT_LIMIT_BYTES` (default 256000). Expired records are
removed according to `APPRENTICE_TRACE_RETENTION_DAYS` (default 30).

By default, inputs and outputs are represented only by type and serialized size.
Set `APPRENTICE_TRACE_CONTENT=true` only when you intentionally want prompts,
learner responses, model outputs, and scenario state stored locally. These can
contain private professional information. Common secret/authentication fields are
recursively redacted, but no automatic redactor can recognize every sensitive
business detail. Keep the server bound to `127.0.0.1`; Observer also rejects
non-loopback clients and will not start without an owner token.

Observer shows observable decision artifacts, not the model's private hidden
chain-of-thought. Rejected attempts include the validation reason so generator
behavior can still be inspected directly.

### Optional model tracing with Langfuse

Apprentice can send each practice operation and its nested Codex generation to
Langfuse. Tracing is off by default and the base installation does not require the
Langfuse SDK.

```bash
uv sync --extra observability --locked
export APPRENTICE_LANGFUSE_ENABLED=true
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=https://cloud.langfuse.com
uv run uvicorn apprentice.sidecar.app:build_app --factory --host 127.0.0.1 --port 8000
```

The traces group `practice.start`, `practice.respond`, `practice.stop`, and
`practice.debrief` by Apprentice session ID. Child generation observations show
the agent, model, output schema, retry metadata, duration/error status, and the
deterministic action and world result where applicable.

Observer and Langfuse can run together. Enable both sets of environment variables;
the same operation/generation hierarchy is written locally and sent to Langfuse.

Privacy defaults are deliberately conservative. Without further configuration,
trace inputs and outputs contain only types and serialized sizes; learner text,
prompts, and model responses are not recorded. To inspect that content, explicitly
set `APPRENTICE_TRACE_CONTENT=true`. This sends the full structured prompts, learner
transcript, scenario state, and structured model outputs to the configured Langfuse
project. Obvious secret and authentication fields are recursively redacted in both
modes, and Langfuse credentials are never forwarded to the Codex subprocess.

Only observable inputs, structured final outputs, and deterministic state changes
can be traced. Apprentice cannot expose a model's private or hidden chain of thought;
for facilitator turns, the inspectable reasoning is the returned interpretation,
strength, risk, evidence, and selected action.

### Suggested judge walkthrough

1. Enter `Site reliability engineer`.
2. Add `I manage production services and participate in incident response`.
3. Start the practice and review the generated timeline and constraints.
4. Explain a concrete first action and why you chose it.
5. Continue until the debrief.
6. Start another practice with the same field to observe novelty rejection and
   a different generated situation.

### Test without model credentials

```bash
uv sync --locked
uv run ruff check .
uv run pytest -q
```

The automated suite uses deterministic fake model responses, requires no network
access, and currently contains 90 passing tests.

## Routes

Learner interface:

- `GET /`
- `POST /practice`
- `GET /practice/{session_id}`
- `POST /practice/{session_id}/responses`
- `GET /practice/{session_id}/debrief`

Owner interface (only when explicitly enabled):

- `GET /observer`
- `POST /api/observer/session`
- `GET /api/observer/traces`
- `GET /api/observer/traces/{session_id}`

Equivalent JSON API:

- `POST /api/practice/sessions`
- `GET /api/practice/sessions/{session_id}`
- `POST /api/practice/sessions/{session_id}/responses`
- `GET /api/practice/sessions/{session_id}/debrief`

## Current limitations

- The current release is a local, single-user experience.
- A real practice requires a locally authenticated Codex account with access to
  GPT-5.6 Terra.
- Generation and judgment latency depend on the local Codex session.
- Sessions are durable, but concurrent submissions and multi-user isolation are
  not yet hardened.
- Novelty detection is deliberately bounded; after three rejected generations,
  the app returns a safe error.
- Learning outcomes have not yet been evaluated with a learner study.

## Hackathon submission notes

- **Category:** Education
- **Repository:** <https://github.com/Ami-Khokhar/Apprentice>
- **Build Week requirements:** [overview](https://openai.devpost.com/) ·
  [official rules](https://openai.devpost.com/rules)
- **Demo video:** submitted separately as a public YouTube video.
- **Codex session:** the primary thread's `/feedback` Session ID is supplied in
  the Devpost submission form.
