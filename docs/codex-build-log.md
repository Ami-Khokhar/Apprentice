# AI build log

> **A note on this filename.** The original implementation plan
> (`docs/superpowers/plans/2026-07-10-apprentice-harness.md`) named this file
> `docs/codex-build-log.md`, anticipating that the build would use Codex.
> That did not happen. The filename is kept because the plan references it
> by that name, but everything below is a truthful account of how this
> project was actually built: with **Claude Code**, not Codex.

## How this was actually built

Apprentice was built end-to-end with Claude Code using subagent-driven
development: for each of the eight tasks in the implementation plan, a
fresh implementer subagent — with no memory of prior tasks beyond the
plan, the design spec, and the current state of the repository — was
given that task's brief and file list and asked to implement it against
the existing codebase. After each task's implementation, a separate
adversarial review pass (a fresh reviewer, not the implementer) audited the
diff against the task's stated acceptance criteria, looking specifically
for gaps between what the code claimed to do and what it actually did.

Test-driven development was used throughout: each task's brief calls for
writing a failing test before the implementation that makes it pass, and
the adversarial reviews checked for this (real, behavior-asserting tests
that would fail if the underlying guarantee were removed) rather than
tests that vacuously pass regardless of implementation.

### Real defects the adversarial review process found and fixed

This process caught concrete, non-cosmetic bugs before they shipped —
not hypothetical review notes, but defects that would have broken the
product's central safety claim if left in place:

- **A playbook-contract divergence** between what the induction module's
  validated `Playbook` schema declared (the plan vocabulary: `navigate`,
  `fill`, `upload`, `click`, `commit`, with a fixed action/effect mapping)
  and what a downstream consumer actually expected, caught before the twin
  and replay layers were built on top of an inconsistent contract.
- **A fail-closed gap in replay anchor drift handling**: an early version
  of the production replayer let a raw Playwright error (from a locator
  that no longer resolved against a live, drifted target) escape past the
  replay boundary instead of being caught and turned into a clean
  `anchor_not_found` abort — the difference between a run that fails safely
  into `failed` with zero mutations, and one that could hang or crash with
  a run stuck mid-`executing`.
- **An SSE wiring bug** in the trust dashboard's live-update stream: the
  server-sent event name the endpoint published and the name the
  dashboard's `EventSource` listener subscribed to had drifted apart, so
  the dashboard would silently stop reflecting live changes despite the
  connection itself looking healthy.

### A defect found during this task (Task 8)

Writing the automated end-to-end proof in `tests/e2e/test_demo_arc.py` —
which, unlike any single-component test before it, drives a real twin
rehearsal's *actual* recorded action plan through the *real* production
replayer end-to-end — surfaced a fourth defect of the same class: the
twin's `navigate` tool recorded only `{"path": ...}` for its action, while
`ProductionReplayer._navigate` unconditionally read `action.arguments["anchor"]`
to re-verify the target page after navigating. Every previous test exercised
one side or the other (hand-authored actions with an anchor for replay
tests, or the twin without ever feeding its output into the real replayer),
so nothing had caught the mismatch. It is fixed in
`apprentice/executor/twin_tools.py`: the navigate step's anchor is now
stored alongside its path, exactly like every other step. This is the kind
of gap an isolated unit-test suite structurally cannot see — it takes an
actual, wired-together arc to expose it, which is the whole reason Task 8's
brief calls for one.

### UI iteration

The trust dashboard (capability ladder, evidence counts, promotion
eligibility, run detail, SSE live updates) went through several rounds of
this same implement → adversarially review → fix cycle across the tasks
that built and then extended it, converging on the context-building
functions in `apprentice/sidecar/console.py` returning plain
JSON/Jinja-friendly dictionaries (so tests assert on data directly rather
than scraping rendered HTML) with the template layer kept intentionally
thin.

## What "adversarial review" meant in practice

Each review pass was instructed to assume the implementation might be
subtly wrong in ways that would only surface under a hostile or unusual
input, and to verify claims against the actual code and actual test
output rather than against the implementer's own summary of what it did.
Findings were only reported after being independently confirmed by
reading the relevant code and, where practical, reproducing the failure
mode.
