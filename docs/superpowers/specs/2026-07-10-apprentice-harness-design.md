# Apprentice — Design Spec

**Date:** 2026-07-10
**Status:** Approved in brainstorming; pending final spec review
**One-liner:** An agent that learns your tasks by watching you do them, and earns the right to do them itself — inside a harness where autonomy is graduated, every run is rehearsed in a synthesized twin before touching production, and self-authored capabilities enter on probation.

---

## 1. Problem & thesis

Agent frameworks today offer binary, static permissions: allow / ask / deny, or a global human-in-the-loop toggle. This produces either rubber-stamping (a human approves 200 actions a day without reading) or blind autonomy. There is no path where trust is *built* — the way humans build it: shadow, draft, act with review, act alone, with serious mistakes resetting the clock.

**Apprentice's thesis:** autonomy should be (a) *learned* from demonstration, (b) *earned* per capability through measured performance, (c) *rehearsed* before every production run, and (d) *revocable* automatically. The agent is the protagonist; the harness is the employment contract.

Three fused primitives, each individually novel at the harness layer:

1. **Learn-by-demonstration** — the agent watches you work in an instrumented browser and induces executable playbooks.
2. **Trust graduation** — per-capability autonomy ladder (observe → shadow → suggest → act-with-veto → autonomous) driven by an inspectable trust ledger with promotion, automatic demotion, and decay.
3. **Twin-gated execution** — no first-time execution in production, ever: every novel run must first pass, with invariants, in a twin of the target site synthesized from the agent's own observations.

Plus a fourth module closing the self-improvement loop: **self-authored tools on probation** (adapted from Flowy's write-tool system) — when the agent hits a capability gap, it authors a new tool for itself, and the new tool enters the same ladder at the bottom.

## 2. Scope

**v1 (build week) includes:** single user, single machine; browser + screenshot observation surface; OpenAI Agents SDK execution agent; sidecar harness with console; twin gate; self-authoring with interceptor-level (not OS-level) enforcement; one self-hosted demo target app ("expense portal").

**v1 explicitly excludes (YAGNI):** multi-user/multi-tenant; cloud deployment; model fine-tuning; full-desktop / computer-use observation; OS-level sandboxing of self-authored tools; Chrome-extension recorder for the user's everyday browser; second demo domain pack; auto-segmentation of continuous browsing into tasks.

**Architectural commitment:** the harness core (interceptor, ledger, ladder, twin machinery, probation) is 100 % domain-agnostic. It sees tool calls, manifests, and outcome events — never domain semantics. Domain specifics live in a thin outer ring: a tool pack, seed traces, and demo narrative.

## 3. System architecture

Approach chosen: **sidecar runtime** (over in-process library and model-layer interception).

```
┌──────────────────────────────┐      ┌─────────────────────────────────┐
│  Instrumented Chrome (CDP    │      │  Apprentice Sidecar (FastAPI)   │
│  via Playwright); user works │─────▶│  • trace store (demos, HAR)     │
│  here; recorder streams      │      │  • trust ledger (SQLite)        │
│  events + DOM + screenshots  │      │  • decision engine (ladder)     │
└──────────────────────────────┘      │  • twin builder + invariants    │
                                      │  • veto queue (server-side      │
┌──────────────────────────────┐      │    countdown timers)            │
│  Agent (OpenAI Agents SDK)   │◀────▶│  • console (SSE, server-       │
│  • playbook induction        │      │    rendered UI)                 │
│  • playbook execution        │      └─────────────────────────────────┘
│  • tool authoring            │
└──────────────────────────────┘   every tool call → sidecar verdict:
                                   execute / propose / delay+veto / deny
```

- The **sidecar owns all state**: traces, playbooks, ledger, verdicts, approvals, drafts. Veto countdowns are server-side timers, so pending actions survive agent restarts.
- The **agent is stateless-ish**: reads playbooks and trust levels from the sidecar; every action flows through the sidecar's interceptor via a thin shim decorator on each SDK tool.
- Failure mode by construction: kill the agent, nothing is lost; kill the sidecar, the agent cannot act at all.

**Rejected alternatives (recorded for posterity):** in-process library (harness dies with agent; countdowns not durable; weaker product story); model-layer interception of `tool_calls` (fights SDK internals, fragile, murky outcome attribution).

## 4. Observation layer

- **Recorder:** a dedicated Chrome instance launched under CDP via Playwright. The user performs tasks in this window when they want Apprentice watching. Single codepath: the same CDP channel later executes the agent's actions (observe/act symmetry — the agent may only act in an action space it can also observe; this is what makes shadow-diffing possible).
- **Demonstrations are explicit:** user hits *record* in the console, names the task, works, hits *stop*. No auto-segmentation in v1.
- **Captured per step:** timestamp; event type (click / input / navigate / submit / scroll); robust element anchor (accessibility role + name, CSS fallback); URL; trimmed accessibility-tree excerpt around the target; screenshot reference. **Network traffic (HAR)** is captured per demonstration — this feeds the twin (§6).
- **Redaction from day one:** password/OTP input types never captured; per-site denylist skips recording entirely; captured values can be marked sensitive in the console before induction.

## 5. Playbook induction

After ≥2 demonstrations of a task, an LLM pass over normalized traces produces a versioned **playbook**:

```yaml
task: file expense report
goal: submit an expense in the portal with receipt attached
inputs: [amount, merchant, receipt_file]   # detected by diffing demos:
                                           # varies across demos = parameter;
                                           # invariant = procedure
preconditions: [logged into portal]
steps:
  - intent: open new expense form          # intent-level, not pixel-level
    anchor: {role: button, name: "New Expense"}
  - intent: fill amount field with {amount}
decision_points:
  - when: "expense > 1000"
    observed: "user added a justification note"
success_criteria: confirmation page shows expense ID
needed_tools: []                           # gaps → self-authoring flow (§8)
```

Key mechanisms:

- **Multiple demonstrations separate procedure from parameters.** One demo is a macro; several demos with different inputs reveal which values vary (inputs) and which steps are invariant (procedure).
- **Decision points come from divergence** between demonstrations — where the user's runs branched, the playbook records a rule, not a step.
- Steps inexpressible as browser actions land in `needed_tools` and queue a capability request.
- Each induced playbook immediately becomes a **capability bucket at L0**. Re-induction after new demonstrations bumps the playbook version and resets the bucket to L1 (new procedure, new exam).

## 6. Execution, shadow mode, and the twin gate

### 6.1 Execution

An SDK agent receives the playbook as instructions plus browser tools (`navigate`, `click(anchor)`, `fill(anchor, value)`, `read_page`, `screenshot`) acting through the recorder's CDP channel. Anchors re-resolve against the live accessibility tree (generalization, not macro replay). Decision points are explicit branches; the agent logs its reasoning per branch (reviewed at L2; sampled by audits at L4).

### 6.2 Two-tier gating

1. **Task-level:** one verdict per run from the bucket's ladder position — at L2 the run is a proposed plan; at L3 it executes after a veto countdown.
2. **Commit-step floor:** irreversible action classes (submits that send / pay / publish externally; deletes) retain their own veto window inside otherwise-trusted runs until the bucket clears a higher bar; critical-risk buckets never lose it (§7 caps).

### 6.3 Shadow mode (L1)

- **Replay shadow (offline):** the agent runs against recorded demonstrations — at each step it sees the page state the user saw, chooses an action, and is diffed against what the user did. Every new demonstration doubles as a free exam.
- **Dry-run-to-the-brink (live, fallback only):** real run with all commit-class actions denied; kept only for when HAR coverage is too thin to build a usable twin.

**Diff scoring:** step agreement (intent + anchor + value modulo declared inputs), decision-point agreement, end-artifact similarity — weighted by step criticality. Scores land in the ledger as events.

### 6.4 Twin-gated execution ("rehearsal")

**Primitive: no first-time execution in production, ever.** In v1 the rule is maximally simple: **every production run, at any trust level, must be immediately preceded by a passing rehearsal of that exact run (same playbook version, same inputs) in a synthesized twin.** Rehearsals are offline and cheap, so there is no reason to skip them; per-input-class rehearsal caching is a v2 optimization, deliberately not in scope.

Twin construction (from artifacts already recorded):

1. A Playwright context serving recorded responses from the demonstration HAR (`routeFromHAR`) — pages, scripts, and API reads work offline.
2. **Mutating requests (POST/PUT/DELETE) are intercepted and never leave the machine**; the twin answers with a synthesized plausible response patterned on observed traffic; the captured outbound payload is the artifact under test.
3. Unmatched requests fall through to an LLM-backed mock responder that improvises schema-consistent answers, always flagged "improvised."

**Rehearsal invariants:** commit-payload matches declared inputs (this amount, this recipient); no requests to hosts unseen in any demonstration; playbook success criteria reached in the twin; **no improvised responses on the critical path** — if the twin guessed at a commit step, the rehearsal doesn't count and the run escalates to a human. Pass → real run proceeds at its trust level's oversight. Fail → failure event, trace to console, zero production contact.

Twin gaps the agent can't cover from recordings are filled by agent-authored mock responders — drafted and reviewable in the console, like tools. The agent assembles its own staging environment.

**Trust × rehearsal are orthogonal axes:** the ladder decides how much human oversight a run gets; the twin gate decides whether this specific run has been rehearsed. Even a fully-trusted L4 playbook rehearses every run first.

## 7. Trust ledger & promotion math

**Design principle: boring, inspectable math.** No learned models decide autonomy; every console number traces to listed events. All thresholds live in `trust_policy.yaml` (demo-tunable).

### 7.1 Schema (SQLite)

```sql
buckets  (id, name, kind,             -- playbook | self_tool | action_class
          risk_class,                 -- low | medium | high | critical
          level,                      -- 0..4
          origin,                     -- demonstrated | self_authored
          playbook_version, created_at)

events   (id, bucket_id, ts, run_id,
          type,     -- shadow_pass/fail, twin_pass/fail, approved,
                    -- approved_with_edits, vetoed, run_success/failure,
                    -- audit_pass/fail
          weight,   -- signal-quality multiplier
          severity, -- failures: minor | major | critical
          detail)   -- JSON: diff score, edit distance, latency…

runs     (id, bucket_id, ts, input_digest, twin_run_id,
          verdict, outcome, trace_ref)

verdicts (id, run_id, action_class, decision, decided_by, ts)
```

### 7.2 Score

Recency-weighted success ratio (Beta-Bernoulli in spirit): successes/failures accumulate as α/β with per-event exponential decay (half-life ≈ 30 days); score = α/(α+β). Failure multipliers: minor 3×, major 8×. **Critical failures bypass the math: instant two-level demotion + console alert.** Asymmetric by design — trust builds slowly, collapses fast.

### 7.3 Ladder & promotion (baseline, medium risk)

| Level | Meaning | Promotion requires |
|---|---|---|
| L0 | Observe only | — (bucket created at induction/activation) |
| L1 | Shadow | playbook induced from ≥2 demonstrations |
| L2 | Suggest (each run approved) | ≥5 shadow runs, score ≥ 0.85 |
| L3 | Act with veto countdown | ≥8 approved L2 runs, score ≥ 0.90, ≤1 veto in last 10 |
| L4 | Autonomous + sampled audits | ≥15 L3 runs, score ≥ 0.95, zero major failures in window |

- **Risk classes scale and cap:** high risk ≈ 2× sample counts and +0.03 on score thresholds; **critical-risk buckets cap at L3** (the commit-step veto never disappears for payments, external sends, deletes).
- **Promotion is proposed, not automatic:** the console shows eligibility + evidence; the user confirms with one click (human stays the accountable party).
- **Demotion is automatic:** score below (promotion threshold − 0.10, hysteresis), a major failure, or a veto-rate spike drops a level immediately. Machine-fast in the unsafe direction, human-slow in the safe one.
- **Decay:** 21 idle days → lose a level. Re-induction → reset to L1.

### 7.4 Signal quality (rubber-stamp defense)

Approvals are weighted by plausible attention: ~0.3× for a sub-2-second zero-edit approval; ~1.2× for open-diff-edit-approve. **Vetoes and failures always count at full weight** — inattention can slow trust-building, never accelerate it. Per-bucket average signal quality is displayed in the console.

### 7.5 Twin events in the ledger

Twin passes gate execution but contribute only ~0.25× positive weight (rehearsals prove the plan; production outcomes prove the agent). Twin failures count at full weight. Improvised-response passes contribute nothing.

## 8. Self-authoring — tools on probation

Adapted from Flowy's write-tool system (skill + selftools runtime), with two substitutions.

- **Trigger:** `needed_tools` from induction, or repeated executor failure diagnosed as a capability gap → the agent files a plain-language **capability request** in the console. Nothing proceeds without user approval (**Gate 1**).
- **Authoring:** adapted write-tool playbook — problem statement and acceptance criteria first, feasibility against real docs, least-privilege contract, security checklist (secrets, network allowlists, path jail, timeouts, idempotent writes). Drafts exactly two files into a jailed directory: `server.py` (small FastMCP server, stdlib-first, pinned deps, real `--selftest`) and `manifest.toml` (declared network hosts, file paths, env names, per-operation `auto`/`confirm` tier, verbatim `approved_request`). Drafts are inert.
- **Review:** console shows complete source + plain-language review; activation displays line count + sha256 of exactly what was reviewed (**Gate 2**).
- **Activation — two substitutions vs Flowy:**
  1. **Selftest runs against the twin, not the live API** (mock synthesized from declared hosts + observed traffic). First live call happens later as an approved L2 action.
  2. **Enforcement is interceptor-level, not OS sandbox:** every tool call passes the sidecar, which rejects undeclared hosts; the tool spawns with a scrubbed environment (declared env names only). Stated plainly: this resists accidents, not adversarial code — the human code review is the adversarial defense. OS sandboxing is the noted v2 hardening.
- **Registration = probation:** bucket `kind=self_tool`, `origin=self_authored`, **risk class derived from the manifest** (read-only/no-network → low; external hosts + mutations → high; payments/sends/deletes → critical). Self-authored origin: promotion sample counts ×1.5; entry always at L1.
- **Ladder for tools:** L1 — callable only inside twin rehearsals. L2 — live calls, each approved. L3 — `auto` operations free; `confirm` operations get the veto countdown. L4 — free within declared authority, sampled audits. Critical caps at L3. (Flowy's static tiers become per-operation floors under an earned ceiling.)
- **Demotion → disable:** at L0 the sidecar deregisters the tool (Flowy `disable_tool` semantics); dependent playbooks revert to `needed_tools`-pending and drop to L2.
- **Compositional trust rule:** the effective level of a run is `min(playbook level, level of every tool it invokes)`. A playbook is never more trusted than its least-trusted tool.

## 9. Console

Served by the sidecar; server-rendered + SSE (no SPA framework). Five views:

1. **Skill tree (home):** every bucket as a card — ladder position, score sparkline, signal quality, events-to-next-promotion; promotions pulse for confirmation, demotions flash with cause.
2. **Approval queue:** L2 proposals (plan + filled-form screenshots + rehearsal result) and L3 veto countdowns (live timer strip, one-click cancel). Approve / edit-then-approve / veto-with-reason.
3. **Run viewer:** filmstrip per run — step intents, screenshots, twin-vs-real side-by-side, invariant checklist, decision-point reasoning. Doubles as the L4 audit surface.
4. **Recording control:** start/stop/name demonstrations; per-site denylist; mark-sensitive-before-induction.
5. **Tool review:** capability requests; full-source view; manifest summary; sha256 activation confirm.

## 10. Demo plan

**Target:** a self-hosted "expense portal" web app (login, form, file upload, submit, confirmation) built for the demo. Rationale: perfect HAR/twin fidelity, deterministic on stage, no third-party ToS exposure, and failure injectable on cue (e.g., silently reorder a form field).

**7-minute arc:**

1. **Teach** (60 s) — record filing an expense twice; show the induced playbook with auto-detected inputs.
2. **Shadow** (60 s) — replay-shadow runs stream in; skill tree reaches L2 eligibility; user confirms promotion.
3. **Rehearse** (90 s) — new expense; split screen: twin run executes the whole task *including Submit*; invariants go green. "It has never touched production, and it has already proven the run."
4. **Act** (90 s) — real L3 run: fills the form, pauses at the commit-step floor, veto countdown ticks, user lets it pass; confirmation page; ledger ticks up.
5. **Grow** (60 s) — task variant needs a non-browser lookup; capability request appears; pre-drafted tool reviewed, activated, first run inside the twin. *(Cut first if time-constrained.)*
6. **The fall** (60 s) — injected site change; twin rehearsal fails invariants; production untouched; bucket demotes on screen. Close: "Autonomy that's earned, rehearsed, and revocable."

## 11. Testing strategy

- **Ledger math — exhaustive unit + property tests** (the safety kernel): failures outweigh symmetric successes; signal quality never accelerates promotion; demotion hysteresis; compositional-min rule.
- **Induction & shadow-diff — golden fixtures:** corpus of recorded traces with expected playbooks and diff scores; every prompt tweak reruns the corpus; LLM outputs judged on structure + key fields.
- **Twin — fidelity tests:** recorded flows serve completely; **mutating requests never escape (asserted at the network layer, not trusted)**; improvised responses are flagged.
- **End-to-end — the demo script, automated:** beats 1–6 as a Playwright integration test against the expense portal. If it can't pass in CI, it can't pass on stage.
- **Interceptor — adversarial unit tests:** undeclared host; undeclared env var; commit-class action at every level; tool call during L1 — each must be denied.

## 12. Build-week shape (indicative)

- **Days 1–2:** sidecar skeleton — interceptor, ledger, decision engine, `trust_policy.yaml`; recorder (CDP capture incl. HAR); expense portal app.
- **Day 3:** induction pipeline + replay shadow + diff scoring; golden fixtures started.
- **Day 4:** twin builder + invariants; execution agent with two-tier gating.
- **Day 5:** console (skill tree, approval queue, run viewer); self-authoring module.
- **Day 6:** demo script end-to-end, failure injection, automated demo test.
- **Day 7:** polish, rehearse, cut what isn't landing (beat 5 first).

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Bucket-smuggling (risky action classified into trusted bucket) | Gate on canonical tool + argument-pattern signatures, not fuzzy classification; unknown patterns default to L2 |
| Rubber-stamping degrades approval signal | Signal-quality weighting (§7.4); visible per-bucket |
| Reward hacking (agent learns what gets approved) | L4 sampled audits deeper than L2 review; audit failures weighted major |
| Twin infidelity on token/nonce-heavy sites | LLM fallback responder + "improvised on critical path = rehearsal void" rule; demo on self-hosted portal |
| HAR replay gaps | Dry-run-to-the-brink fallback (§6.3) |
| Self-authored tool exceeds declared authority | Interceptor host/env enforcement + human full-code review; OS sandbox deferred to v2, stated honestly |
| Induction quality on few demos | Minimum 2 demos for L1; re-induction resets to L1; golden-fixture regression on prompts |
| Live-demo flakiness | Self-hosted target; automated demo test in CI; demo-tuned `trust_policy.yaml` |

## 14. Open items (deferred, not blocking)

- Chrome-extension recorder for everyday browsing (v2).
- OS-level sandbox for self-authored tools (v2 hardening).
- Multi-agent shared harness (architecture supports it; not built in v1).
- Exact LLM prompts for induction, diff scoring, and mock responder — defined during implementation against the golden fixtures.
