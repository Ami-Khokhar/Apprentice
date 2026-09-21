# Codex development and Terra runtime

Apprentice was developed with Codex using GPT-5.6 Sol and a subagent-driven
workflow. The product is intentionally limited to one job: generating and
facilitating professional judgment simulations.

## Development workflow

Codex split implementation into bounded simulation, interface, persistence,
testing, and review tasks, then reconciled the results in the shared workspace:

1. define typed generated-world and practice-session contracts;
2. separate model facilitation from deterministic world authority;
3. build the Japanese-dojo server-rendered interface;
4. execute Terra through the locally authenticated Codex CLI;
5. harden the CLI subprocess environment and disable tools for practice turns;
6. add accessible loading states and desktop/mobile rendered QA;
7. add semantic novelty rejection, bounded retries, persistence, and replay; and
8. verify the system with automated tests and real local runs.

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

Terra has four bounded responsibilities:

- invent one structured professional stress-test specification from the
  learner's field and optional work context;
- interpret a learner's free-text response as at most one immediate action or a
  defensible unmodelled decision;
- answer factual clarification questions from currently observable evidence;
  and
- write a grounded debrief from the frozen world and learner transcript.

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

The suite covers deterministic simulation, semantic validation, duplicate
rejection, restart restoration, facilitator evidence boundaries, clarification,
profiles, observability, API behavior, and dojo templates. The keyless
Codex/Terra path was also exercised against the real locally authenticated CLI.
