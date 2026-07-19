# Demo script

A step-by-step presenter script for recording the Apprentice demo video,
using the real dashboard and a headed browser via `scripts/run_demo.py`.
It is mapped to the brief's video priority order:

1. teach
2. held-out proof
3. twin rehearsal
4. approval and exact production replay
5. changed-site failure with unchanged mutation count
6. demotion

## Before recording

```bash
uv sync
uv run playwright install chromium
```

Rehearse the whole script at least once, solo, before recording (per the
plan's definition of done). Run:

```bash
uv run python scripts/run_demo.py
```

This opens the dashboard in your default browser automatically and prints a
narrated log to the terminal. Arrange your screen so the terminal and the
dashboard browser tab are both visible (a split screen or a terminal
overlaying part of the browser works well) — the terminal explains what is
about to happen; the dashboard and the (headed) Playwright browser window
show it actually happening.

## 1. Teach (script prints "Induced playbook...")

- **Say:** "Apprentice learns a task from two human demonstrations — no
  code written for this task, just two recorded runs of filing an expense."
- **Show:** the terminal line `Induced playbook: 'file expense', 6 steps`.
- **Point to:** `fixtures/expense/expected_playbook.json` in an editor —
  the typed, human-readable steps, inputs, and the one decision point
  (amount over 1000 requires justification) GPT-5.6 induced from the
  variance between the two demonstrations.
- **Show:** the dashboard's capability card for `file-expense` appearing at
  **L1** the moment the script prints "Capability activated at L1".

## 2. Held-out proof (script prints "Held-out shadow evaluation: shadow_pass")

- **Say:** "Before it's trusted to act, it has to pass on an example it
  never trained on — evidence that can't be replayed or gamed, because it's
  scored against the held-out demonstration's own recorded ground truth,
  not against anything the model can see in advance."
- **Show:** the dashboard card's evidence count ticking up
  (`shadow_pass: 1`) and the capability promoting to **L2** the moment the
  script prints "Promoted to L2 (approval required)".

## 3. Twin rehearsal (script prints "Rehearsal checks: {...}")

- **Say:** "Before any run reaches production, it's rehearsed in an offline
  twin: a fresh, read-only snapshot of the real target, merged over the
  recorded demonstration. The twin captures what a commit *would* do — it
  never forwards it."
- **Show:** the terminal's six named checks, all `True`. Click into the
  run's detail page on the dashboard (`/console/runs/<run_id>`) and show the
  same six-check list rendered there, plus the stored action plan.

## 4. Approval and exact production replay

- **Say:** "This capability is at L2: nothing executes without a human
  decision." When the terminal prints "Run ... is approval_pending (L2)...",
  **click Approve on the dashboard** (not the terminal) — this is the live
  human-in-the-loop moment.
- **Show:** the **headed Playwright browser window** that opens and fills
  the real expense form, submits it, and the confirmation page — this is
  the *exact* stored plan replaying against production, not a new decision
  by the model.
- **Show:** the production portal's mutation counter (visible via
  `GET /api/debug/mutations`, or narrate it from the terminal) going from
  `0` to `1`, and never higher, for this one run.
- **Repeat once more** (the script runs a second approved success
  automatically) to show the capability promote to **L3**.
- **Show L3's durable veto window**: when the terminal prints "Run ... is
  in its L3 veto window (~5s)...", either let it expire (auto-authorizes,
  narrate "no objection, it proceeds") or click **Cancel** on the dashboard
  to show a veto instead (rehearse both outcomes solo beforehand; pick
  whichever tells the better story for the take you keep).

## 5. Changed-site failure with unchanged mutation count

- **Say:** "Now the site changes underneath it — the same address, but the
  form's commit contract silently changed a field name, exactly the kind of
  drift a real site redeploy could cause."
- **Show:** the terminal's rehearsal checks for this run, with
  `commit_payload_matches_inputs: False` and everything else still `True` —
  it isn't a crash, it's a specific, named check catching a specific drift.
- **Show:** the restarted changed-site portal establishing mutation baseline
  `0`, then remaining at `0` after the failed rehearsal. The earlier healthy
  instance reached `3`; its in-memory counter resets when the intentionally
  changed instance replaces it at the same address.

## 6. Demotion

- **Say:** "The moment that happens, the capability loses trust
  automatically — no one has to notice and manually revoke it."
- **Show:** the dashboard capability card dropping from L3 to L2, with the
  visible demotion reason shown on the card (severity and cause).

## Wrap

- **Say the product sentence:** "Apprentice watches you perform a task,
  turns the demonstrations into a semantic playbook, rehearses each new run
  in an offline twin, and earns narrowly scoped autonomy from verified
  evidence."
- End on the dashboard showing the full capability history: L1 → L2 → L3 →
  L2, each transition backed by a concrete, inspectable piece of evidence.

## Timing notes

- The L3 veto window is 5 seconds (`trust_policy.yaml`'s `veto_seconds`) —
  don't cut it from the recording; the countdown itself is part of the
  proof that L3 autonomy is revocable, not instantaneous.
- Total narrated runtime, unhurried, is roughly 4-6 minutes; trim narration
  rather than the on-screen evidence (checks, counters, dashboard state)
  when fitting a video-length limit.

## Before submission (human-only)

- Re-check the OpenAI Build Week / Devpost challenge rules, tracks,
  required materials, and video length limit after July 13, and adjust this
  script's length/order if the requirements differ from what's assumed
  here.
- Record the final take from a clean checkout following the README's
  three-command run, not from a machine with leftover demo state.
