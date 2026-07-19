# Codex development and Terra runtime

The original Apprentice trust harness predates the professional-judgment dojo;
its build history remains documented separately in `docs/codex-build-log.md`.
The dojo product evolution was developed with Codex using GPT-5.6 Sol and a
subagent-driven workflow.

## Development workflow

Codex was used to inspect the existing architecture, split implementation into
bounded backend, simulation, interface, testing, and review tasks, and reconcile
the results in the shared workspace. The work proceeded in progressive slices:

1. preserve the existing trust harness behind `/legacy`;
2. introduce typed practice-session contracts and durable SQLite storage;
3. separate model facilitation from deterministic world authority;
4. build the Japanese-dojo server-rendered interface;
5. replace API-key execution with the locally authenticated Codex CLI;
6. harden the CLI subprocess environment and disable tools for practice turns;
7. add accessible loading states and desktop/mobile rendered QA;
8. replace the finite authored selector with validated Terra-generated worlds;
9. add semantic novelty rejection, bounded retries, persistence, and replay; and
10. verify the final system with the full automated suite and real local runs.

Implementation subagents worked on isolated concerns while separate review
passes looked for prompt-injection exposure, credential inheritance, structured
output failures, semantic duplication, impossible action graphs, unsafe model
authority, and UI regressions. Confirmed findings were converted into focused
regression tests before final integration.

## GPT-5.6 Terra inside Apprentice

The dojo uses `gpt-5.6-terra` through `codex exec`, authenticated by the local
ChatGPT/Codex login. It does not require `OPENAI_API_KEY` for the learner flow.
The subprocess receives a minimal environment and has shell, browser, apps,
plugins, image generation, multi-agent tools, and web search disabled.

Terra has two bounded responsibilities:

- invent one structured professional stress-test specification from the
  learner's field and optional work context; and
- interpret a learner's free-text response as at most one currently enabled
  action, then write a grounded debrief.

Terra does not mutate a running simulation. Generated facts, metrics, evidence,
actions, prerequisites, effects, escalation events, success requirements, and
rubric references are validated and frozen first. A deterministic engine owns
all subsequent transitions and outcomes.

## Novelty guarantee

Each generated world receives a semantic fingerprint derived from its failure
mechanism, decision trade-off, evidence, action graph, effects, and escalation
path while excluding cosmetic identifiers and title wording. Prior fingerprints
and causal summaries are persisted by professional field. Exact or near
duplicates are rejected and regenerated up to three times; Apprentice returns a
safe error instead of presenting a repeated world when novelty cannot be
established.

Real verification with the same AI-engineer profile produced materially distinct
worlds: a contaminated retrieval corpus before a policy release, followed by
hidden-text prompt injection in uploaded support logs. Their evidence, trade-offs,
actions, consequences, and semantic fingerprints differed, and a real learner
decision advanced the second generated action graph.

## Verification

The default suite covers the deterministic engines, semantic validation,
duplicate rejection, restart restoration, facilitator evidence boundaries,
legacy compatibility, API behavior, and dojo templates. The final development
run completed 400 tests with two explicitly marked live API-key tests deselected;
the dojo's keyless Codex/Terra path was additionally exercised against the real
locally authenticated CLI.
