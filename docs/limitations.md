# Limitations

This is a Build Week vertical slice, not a production system. It
intentionally narrows scope so the central safety claim — an agent only
ever executes a stored, rehearsed action plan, and loses trust the moment
its target drifts — can be proven completely rather than partially proven
across a wider surface. What follows is a truthful, specific account of
what is deferred and why, so it can be weighed accurately.

## Single domain

The entire harness is demonstrated against one recorded task (filing an
expense in `demo_portal`, a small local FastAPI app built for this project).
The induction prompt, the twin's page parser, and the invariant checks are
general in shape (typed anchors, a fixed six-step action vocabulary,
deterministic evidence validation) but have only ever been exercised against
this one form. Generalizing to arbitrary sites, multi-page flows, or
JavaScript-heavy SPAs is unproven and out of scope here.

## Post-login recording only

`RecorderSession` starts capturing after the user has already signed in; it
is explicitly not presented as a general credential-safe browser recorder.
Authentication itself (the login form, session establishment) is a
precondition supplied by the caller (a `page.goto("/login")` + fill + click
sequence, or a Playwright `authenticate` callback for production replay),
never something the recorder, twin, or replayer captures, rehearses, or
reasons about.

## Local, single-user machine

Everything — the demo portal, the sidecar, the SQLite ledger, the dashboard
— runs as local processes on one machine for one operator. There is no
multi-tenant isolation, no remote deployment story, and no concurrent-user
handling beyond what SQLite's own transaction semantics happen to provide.

## Constrained self-authored tools

The expense-policy demo implements one deliberately narrow self-authoring
path: a detected policy-field gap can draft a declarative, read-only HTTP
tool. The artifact is limited to a manifest and generated server adapter,
is hash-pinned, passes a twin contract test, enters at L1, requires held-out
evidence to reach L2, and needs explicit approval for its first live call.
Schema drift fails closed, demotes the tool, and lowers the effective trust
of dependent playbooks.

Arbitrary Python generation, write-capable tools, generalized tool discovery,
and OS-level sandboxing remain deferred. The included capability-gap detector
recognizes only the expense-policy contract; it is not a general autonomous
tool-authoring system.

## Deferred: L4 audit scheduling

The trust ladder's L4 (autonomous) tier is reachable in the policy and the
approval boundary (`ApprovalService.begin_activation` authorizes
immediately at L4), but there is no scheduled or triggered *audit* process
that periodically re-verifies an L4 capability's continued fitness once
promoted. Demotion still happens immediately on any severity-carrying
failure event (a rehearsal, replay, or shadow failure), which is the safety
property this build actually proves; a proactive audit cadence for
otherwise-quiet L4 capabilities is deferred.

## Deferred: multi-domain / cross-capability composition

Tool-kind buckets and `effective_level` (a capability can never execute
above its weakest declared dependency) are exercised by the policy-aware
expense demo. Composition across multiple tools or domains, and reuse of one
capability's trust across domains, remain unproven.

## Live GPT-5.6 verification deferred

No `OPENAI_API_KEY` was available during this build. Every test in the
default suite (`uv run pytest -q`) uses an injectable, deterministic fake
runner in place of every GPT-5.6 call (induction, held-out shadow
evaluation, and the twin's rehearsal agent) — this proves the deterministic
half of the system (validation, invariants, the ledger, the approval
boundary, and exact replay) completely, but it does not exercise the real
model's actual behavior.

Two tests are marked `@pytest.mark.live` and excluded from the default run
(`tests/executor/test_twin_agent.py::test_live_gpt_5_6_completes_the_expense_task_inside_the_twin`
and `tests/induction/test_induce.py`'s live induction test). **Before
submission**, these must be run with a real key and network access
(`OPENAI_API_KEY=... uv run pytest -m live`), and — per the brief — the
normal and high-amount eval-corpus cases should additionally be run once
against real GPT-5.6 with the Agents SDK's own trace inspection (model
calls, tool calls, the approval interruption, and the final outcome), with
trace identifiers or screenshots saved for the demo notes (never committing
secrets). This was not done as part of this implementation task because no
API key was available in this environment.

The provider-neutral live path has been verified end to end with Gemini 2.5
Pro through Vertex AI: live induction, held-out shadow evaluation, batched
twin tool execution, two L2 approvals, L3 veto expiry, exact production
replay, changed-site rejection, and automatic demotion all completed in one
integrated run. GPT-5.6-specific verification and OpenAI-hosted trace evidence
remain deferred until an OpenAI API key is available.

## Demo-scale trust policy

`trust_policy.yaml`'s thresholds (e.g., one unique shadow pass for L2, two
approved successes for L3, a 30-day trust half-life, a 21-day idle-decay
window, a 5-second veto countdown) are calibrated to make a short, legible
demo arc possible, not to reflect a production risk tolerance. A real
deployment would need domain-specific calibration of every threshold in
that policy, almost certainly with materially higher evidence requirements
before autonomy increases and a veto window measured in a unit larger than
seconds.
