# Apprentice

Apprentice is a minimalist professional judgment dojo. Describe the work you
do—or want to do—and practice the difficult decisions that role is likely to
present in a calm, focused simulation.

## How the dojo works

The learner enters a field and may add context about their current or future
work. The locally authenticated Codex CLI then:

1. invents a new situation for that learner's field and work context;
2. returns its facts, evidence, action graph, escalation clock, and recovery
   conditions as a validated structured specification;
3. interprets each free-text decision and provides focused facilitation; and
4. produces an evidence-backed final debrief.

Terra invents each world once, but does not control it after generation. The
server validates that the generated action graph is coherent and recoverable,
rejects recent semantic duplicates, and freezes the accepted specification.
A deterministic runtime then owns facts, available actions, timeline
transitions, metrics, evidence, and outcomes. Codex can map a learner's intent
to one currently valid action or ask for clarification, but only the runtime
can apply consequences.

## Minimal learner flow

The interface follows a four-screen Japanese-dojo-inspired flow with generous
spacing and one primary task at a time:

1. **Enter the dojo** — describe a professional field and, optionally, the work.
2. **Briefing** — understand the role, timeline, constraints, and first decision.
3. **Practice** — explain what you would do and why, then respond as the situation develops.
4. **Debrief** — review strengths, missed evidence, risky assumptions, a better
   path, and one principle to carry forward.

## Run the dojo locally

The live practice flow uses the Codex CLI and its local ChatGPT sign-in. It does
not read or require `OPENAI_API_KEY`. Install Codex and sign in once if needed;
`codex login status` shows whether the current machine is ready.

```bash
codex login
uv sync
uv run uvicorn apprentice.sidecar.app:build_sidecar --factory --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. The local SQLite database is stored as
`apprentice.db` unless a different path is supplied when building the app.
Each AI turn invokes `codex exec` with a read-only sandbox, the explicit
`gpt-5.6-terra` model, and a structured output schema. Authentication comes
from the local Codex login rather than an application API key.

The development workflow and the boundary between Codex GPT-5.6 Sol and the
in-product GPT-5.6 Terra runtime are documented in
[`docs/codex-dojo-development.md`](docs/codex-dojo-development.md).

For v1, practice sessions are single-user and local. Incident transitions and
practice-transcript persistence are separate writes, so concurrent submissions
and crash recovery are not yet hardened.

## Routes

The server-rendered experience uses:

- `GET /` — profile intake.
- `POST /practice` — create a tailored practice session.
- `GET /practice/{session_id}` — briefing and evolving free-text practice.
- `POST /practice/{session_id}/responses` — submit the next decision.
- `GET /practice/{session_id}/debrief` — final debrief.
- `GET /legacy` — the original browser-autonomy dashboard.

The equivalent JSON API uses:

- `POST /api/practice/sessions`
- `GET /api/practice/sessions/{session_id}`
- `POST /api/practice/sessions/{session_id}/responses`
- `GET /api/practice/sessions/{session_id}/debrief`

## Legacy harness

The original trust-and-rehearsal browser-agent harness remains available for
reference and demonstrations under `/legacy`.

### The problem

Letting an AI agent drive a browser through a real business task (filing an
expense, submitting a form, anything with a side effect) is genuinely useful,
but today it's all-or-nothing: either a human clicks every button, or the
agent gets a live browser and you hope it doesn't misclick, hallucinate a
response, or submit the wrong amount. There is no middle ground where an
agent earns the right to act on its own, one verified step at a time, and
loses that right the moment its target changes underneath it.

### The solution

**Apprentice watches you perform a task, turns the demonstrations into a
semantic playbook, rehearses each new run in an offline twin, and earns
narrowly scoped autonomy from verified evidence** — production only ever
executes an action plan that already passed rehearsal, byte-for-byte.

### Architecture

```
 ┌────────────┐   two demos    ┌───────────────┐   reviewed    ┌─────────────┐
 │  Recorder   │ ─────────────▶ │  Induction    │ ─────────────▶│   Ledger     │
 │ (Playwright,│  + held-out    │ (GPT-5.6,     │   playbook    │  (SQLite):   │
 │  post-login)│  demonstration │  typed, plan- │               │  buckets,    │
 └────────────┘                │  validated)   │               │  runs,       │
        │                      └───────────────┘               │  events,     │
        │ sanitized fixtures            │                      │  trust level │
        ▼                                │ shadow eval           └──────┬──────┘
 ┌────────────┐                          │ (held-out, non-              │
 │  fixtures/  │                          │  replayable evidence)        │
 │  expense/** │◀─────────────────────────┘                              │
 └────────────┘                                                          │
                                                                          │ create_run
                                                                          ▼
                          ┌───────────────────────────────────────────────────────┐
                          │                     Twin (offline)                    │
                          │  fresh read-only snapshot merged over the fixture;     │
                          │  GPT-5.6 drives fill/upload/click/commit; a captured   │
                          │  mutation is never forwarded. Six named checks decide  │
                          │  pass/fail. A pass stores the exact ordered action plan│
                          └───────────────────────────┬───────────────────────────┘
                                                       │ stored action plan (hash-pinned)
                                                       ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │      Approval / veto boundary (L1-L4, by trust level)     │
                     │   L1 denied · L2 human approval · L3 durable veto window  │
                     │   · L4 autonomous --- re-verifies stored evidence first   │
                     └───────────────────────────┬─────────────────────────────┘
                                                  │ authorized
                                                  ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │        Production replayer (real Playwright browser)      │
                     │  replays ONLY the stored plan; whole-replay mutation      │
                     │  guard forwards the one expected commit, exactly once     │
                     └───────────────────────────┬─────────────────────────────┘
                                                  │ verified outcome
                                                  ▼
                     ┌─────────────────────────────────────────────────────────┐
                     │   Trust scoring: promotion / immediate demotion / decay    │
                     │        Dashboard (SSE): capability ladder, evidence,       │
                     │              approvals, run detail, live updates          │
                     └─────────────────────────────────────────────────────────┘
```

Everything above the approval boundary only ever talks to a **twin**: a
recorded fixture merged with one fresh read-only snapshot. Nothing crosses
into production until a stored, hash-pinned action plan has already passed
six named invariant checks and cleared its trust-level's approval gate.

### Run the legacy demo (three commands)

```bash
uv sync
uv run playwright install chromium
uv run python scripts/run_demo.py
```

This starts a local expense portal and the Apprentice sidecar, opens the live
trust dashboard in your browser, and walks the full arc — teach, held-out
proof, twin rehearsal, L2 approval, L3 veto, a changed-site failure, and
demotion — using a fake, deterministic model response (no API key needed).
Pass `--live` (with `OPENAI_API_KEY` set) to drive it with the real GPT-5.6
Agents SDK instead. Pass `--headless` to run without a visible browser
window (useful for a quick sanity check with no display). Run
`uv run python scripts/run_demo.py --help` for all options.

### Inference providers

OpenAI remains the Build Week default, while Gemini, Groq, and NVIDIA can be
used through their OpenAI-compatible Chat Completions endpoints. Copy
`.env.example` to an ignored `.env`, export its values, and select a provider:

```bash
APPRENTICE_PROVIDER=gemini GEMINI_API_KEY=... uv run python scripts/run_demo.py --live
APPRENTICE_PROVIDER=groq GROQ_API_KEY=... uv run python scripts/run_demo.py --live
APPRENTICE_PROVIDER=nvidia NVIDIA_API_KEY=... uv run python scripts/run_demo.py --live
```

`APPRENTICE_MODEL` overrides the provider default, and
`APPRENTICE_BASE_URL` supports another OpenAI-compatible deployment. The
defaults are GPT-5.6, Gemini 2.5 Pro, GPT-OSS 120B on Groq, and Nemotron 3
Super 120B on NVIDIA. Non-OpenAI runs disable the SDK's OpenAI-hosted trace
exporter; use OpenAI for the trace evidence requested by the Build Week brief.

### What the demo proves

- Two demonstrations produce a typed, human-reviewable playbook (`induced
  Playbook v1`), and a held-out example GPT-5.6 never trained on earns the
  one piece of non-replayable evidence required to promote to L2.
- A new run's action plan is rehearsed in an offline twin — against a real,
  fresh, read-only snapshot of the target merged over the recorded fixture —
  and only a passing rehearsal is ever stored.
- Production replays *exactly* that stored plan: the mutation counter stays
  at zero until an L2-approved (or L3 veto-expired) run executes, and it
  increments by exactly one per successful run, never more.
- A meaningful site change (the commit endpoint's field name silently
  drifted) fails rehearsal cleanly, at the same address the capability was
  trained against, and the production mutation counter never moves.
- The capability automatically demotes one trust level with a visible
  reason on the dashboard the moment that happens.
- Forged rehearsal evidence, duplicate held-out evidence, an unseen host, an
  unmatched mutation response, a wrong commit amount, a tampered replay
  plan, a canceled veto, and a restarted-process veto are all rejected —
  see `tests/e2e/test_demo_arc.py` for the automated proof of each.

### Tests

```bash
uv run ruff check .
uv run pytest -q
```

The default suite is fully deterministic: every model call is replaced by an
injectable, fake runner, and the L3 veto window is expired by an injected
clock rather than a real sleep. It requires no `OPENAI_API_KEY`. Two tests
are marked `live` and excluded by default (`addopts = "-m 'not live'"` in
`pyproject.toml`); they exercise the real Agents SDK and require the selected
provider's key and network access. For example, with OpenAI:

```bash
OPENAI_API_KEY=... uv run pytest -m live
```

The automated end-to-end proof lives in `tests/e2e/test_demo_arc.py`: it
walks all 16 numbered steps of the demo arc against a real running portal
and a real Playwright browser, and asserts all ten required adversarial
properties (forged rehearsal claims, duplicate shadow evidence, unseen
hosts, unmatched mutation responses, wrong commit amounts, tampered replay
plans, canceled and restarted vetoes, zero-mutation failed rehearsals, and
the secret-sentinel scan). The five-case local eval corpus (plus three
regression cases) lives in `fixtures/expense/eval_cases.json` and is
re-checked by `tests/e2e/test_demo_arc.py::test_eval_corpus_matches_expected_facts`
— run it after any change to the induction or shadow prompts.

The constrained self-tool demo needs no model key or network access:

```bash
uv run python scripts/run_tool_demo.py
```

It detects missing expense-policy fields, drafts and hash-pins a read-only
lookup tool, proves L1 denial and L2 approval, files one policy-aware expense,
then injects schema drift to demonstrate tool demotion and composed-trust
fallback.

### Repository layout

- `apprentice/` — the harness: recorder, induction, ledger, twin, executor,
  sidecar (approval boundary, production replayer, dashboard).
- `demo_portal/` — a small, local, observable expense portal with injectable
  contract-drift failure modes, used for both fixture recording and replay.
- `fixtures/expense/` — sanitized recorded demonstrations, the reviewed
  playbook, and the eval corpus.
- `tests/` — the full test suite, including `tests/e2e/test_demo_arc.py`.
- `scripts/run_demo.py` — the presenter-facing demo driver described above.
- `scripts/run_tool_demo.py` — the self-authored policy-tool lifecycle demo.
- `docs/` — `demo-script.md` (presenter script), `limitations.md`, and
  `codex-build-log.md` (how this was actually built).

### Before submission

- Re-check the OpenAI Build Week / Devpost challenge rules, tracks, required
  materials, and video length limits once submissions open (July 13).
- Run the `live`-marked tests with a real `OPENAI_API_KEY` and inspect the
  Agents SDK traces (see `docs/limitations.md`).
- Record the presenter demo video following `docs/demo-script.md`.
