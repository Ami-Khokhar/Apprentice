# Apprentice

Apprentice is a minimalist AI dojo for practicing professional judgment under
pressure. Describe the work you do—or want to do—and the guide creates a new,
role-specific situation in which you can practice making difficult decisions.

## How it works

1. You enter a professional field and optional work context.
2. GPT-5.6 Terra invents a new stress-test situation tailored to that profile.
3. Apprentice validates and freezes the generated facts, timeline, evidence,
   metrics, actions, consequences, recovery conditions, and assessment rubric.
4. You explain what you would do and why.
5. Terra interprets your intent as at most one currently available action.
6. A deterministic runtime applies the action and advances the situation.
7. When the situation concludes, Terra produces an evidence-grounded debrief
   with strengths, missed signals, risky assumptions, and a stronger path.

The model creates and facilitates the exercise, but it cannot directly change
the running world. Apprentice validates cited evidence before applying any
decision, and only the deterministic runtime owns state transitions and
outcomes.

## New situations for every practice

Generated situations are not selected from a fixed catalog. Each request asks
Terra to invent a complete new scenario from the learner's field and context.
Apprentice compares its semantic fingerprint and causal structure with recent
situations for the same field. A duplicate is rejected and regenerated up to
three times; if novelty cannot be established, the request fails safely instead
of showing a repeated exercise.

## Design

The interface follows a calm Japanese-dojo visual language with generous
spacing and one primary task at a time:

- **Entry** — describe your field and work.
- **Briefing** — understand the timeline, constraints, and first decision.
- **Practice** — respond in your own words as the situation develops.
- **Debrief** — review the evidence and a stronger decision path.

Model calls use a branded loading overlay inside the main screen. The underlying
content remains visible but blurred, so the page stays oriented without exposing
duplicate actions while the guide is working.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Codex CLI with a ChatGPT sign-in

Apprentice invokes the locally installed Codex CLI and does not require an
`OPENAI_API_KEY`. Run `codex login` to complete the browser sign-in flow, and
`codex login status` to check the active authentication method.

## Run locally

```bash
codex login
uv sync
uv run uvicorn apprentice.sidecar.app:build_app --factory --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Practice sessions are stored in `apprentice.db` by default. Pass a different
database path when calling `build_app` if needed.

## Codex and Terra boundary

Each AI turn runs `codex exec` with:

- the explicit `gpt-5.6-terra` model;
- a Pydantic JSON output schema;
- an isolated temporary working directory;
- a read-only sandbox;
- web search and interactive tools disabled; and
- a minimal environment allowlist that excludes application API keys.

Learner content is sent through standard input rather than command arguments.
The Codex process returns only the structured value required for that turn.

## Architecture

```text
Browser
  │
  ▼
FastAPI + Jinja dojo
  │
  ▼
PracticeService ───────────────► SQLite session history
  │
  ├──► Codex CLI / Terra ──────► generated scenario or structured guidance
  │
  └──► GeneratedScenarioRuntime ► deterministic state transition
```

The retained application is intentionally small:

- `apprentice/incident/generated.py` validates generated worlds and applies
  deterministic transitions.
- `apprentice/practice/` owns session contracts, orchestration, and the
  hardened Codex subprocess.
- `apprentice/sidecar/` contains the web routes, dojo templates, CSS, and
  loading behavior.
- `apprentice/database.py` persists practice sessions in SQLite.

## Routes

Learner interface:

- `GET /`
- `POST /practice`
- `GET /practice/{session_id}`
- `POST /practice/{session_id}/responses`
- `GET /practice/{session_id}/debrief`

Equivalent JSON endpoints:

- `POST /api/practice/sessions`
- `GET /api/practice/sessions/{session_id}`
- `POST /api/practice/sessions/{session_id}/responses`
- `GET /api/practice/sessions/{session_id}/debrief`

## Verify

```bash
uv run ruff check .
uv run pytest -q
```

The test suite uses deterministic fake model outputs, so it does not need
network access or model credentials.

Development and runtime boundaries are described in
[`docs/codex-dojo-development.md`](docs/codex-dojo-development.md).
