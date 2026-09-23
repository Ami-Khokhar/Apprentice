<!-- pr-gatekeeper:start -->
# Agent protocol — how to work under pr-gatekeeper

pr-gatekeeper reviews your pull requests automatically. It reads the PR body
(stage 0 lint), runs deterministic checks, calls reviewer and verifier models,
and publishes one decision per round: `MERGE`, `CHANGES`, or `HUMAN`. This
document explains the format you must follow and how to respond to a review.
It applies to any coding agent; no particular harness is required.

## PR body format (required)

The PR body must contain these six level-2 headings, in any order:

| Heading | Rule |
|---|---|
| `## Task` | Contains an issue reference (`#123`, `Closes #123`, or a URL) |
| `## Intent` | At least 1 non-empty sentence |
| `## Why it was needed` | At least 1 non-empty sentence |
| `## Why this approach` | At least 2 sentences |
| `## What changed` | A bullet list. Each bullet starts with a path in backticks |
| `## Proof of work` | At least one fenced code block that contains a line starting with `$ ` |

Rules for `## What changed`:

- It must list **every changed and deleted path** from the diff. The set of
  paths in the body must exactly equal the set of changed paths in the diff —
  missing paths and extra paths are both reported as problems.
- Each bullet starts with a path in backticks, for example:
  `` - `src/app.py`: added retry logic to the request handler ``
- Deleted files belong in the list too, e.g. `` - `src/old.py` (deleted) ``.

Rules for `## Proof of work`:

- At least one fenced code block containing a command line starting with `$ `
  (the prompt-and-command form). Show the real commands you ran and their
  result. When you fix findings in a later round, update this section to prove
  the fix.

## Finding the latest review

After the workflow runs, the PR gets a summary comment. The latest review is
**the last PR comment whose body contains the marker `<!-- pr-gatekeeper `**.
Fetch comments with `gh pr view <N> --json comments` or the API, scan from the
newest backwards, and stop at the first one containing that marker.

## Parsing the hidden JSON

The marker line has the form:

```
<!-- pr-gatekeeper {"round":1,"outcome":"CHANGES","head_sha":"...","findings":[...]} -->
```

- The JSON is the text between `<!-- pr-gatekeeper ` and the closing ` -->`.
- Because GitHub comments may not contain `-->` inside HTML comments, every
  `>` in the JSON is escaped as `\u003e`. When parsing, a literal `>` in a
  finding description is stored as `\u003e` — standard `JSON.parse` /
  `json.loads` handles this automatically and yields `>` again.
- Fields: `round` (number), `outcome` (`"MERGE"`, `"CHANGES"`, or `"HUMAN"`),
  `head_sha` (the commit that was reviewed), and `findings` (array of
  `{severity, path, line, problem, fix}` objects; severity is `"blocking"` or
  `"minor"`).
- If the JSON does not parse, fall back to reading the visible markdown above
  the marker in the same comment.

## What each outcome means

- `MERGE` — approved; pr-gatekeeper merged the PR (squash by default). Nothing
  to do.
- `CHANGES` — the review found problems. Fix them and push (see below).
- `HUMAN` — a human must look at it (protected paths, secrets, too many
  rounds, model disagreement, or a red base branch). Do not retry blindly;
  read the reasons in the comment.

The PR also carries exactly one label: `gatekeeper:approved` (MERGE),
`gatekeeper:changes` (CHANGES), `gatekeeper:needs-human` (HUMAN), and a check
run named `pr-gatekeeper` with the matching conclusion.

## What to do after `CHANGES`

1. Read the latest review (see above) and every blocking finding.
2. Fix the code. Do **not** open a new PR and do **not** create a new branch —
   push to the **same branch** of the existing PR.
3. Update the PR body: keep all six headings, add every newly changed or
   deleted path to `## What changed`, and update `## Proof of work` with the
   commands that prove the fix.
4. Push. The workflow re-runs on push and on body edits. If it does not run
   (see the token rule below), trigger it manually.

## How pr-gatekeeper runs (two jobs)

The review runs in two isolated runners, and the reviewer **never runs your
PR code**:

1. A `gates` job (no secrets) runs the deterministic checks: it executes the
   verify command and the revert probe on your PR checkout and writes the
   results to `gates.json`.
2. A `review` job holds the model API key. It reads your PR head (never
   executes it) and reads `gates.json` instead of re-running your proof
   commands. The reviewer in this job has no `run` tool.

Consequence for `## Proof of work`: the commands are recorded as data, not
re-executed by the reviewer — but the deterministic gates do execute the
repo's real test suite, so the proof section must still show the commands
you actually ran and that they passed.

## Hard limits

- You must **not** edit files under `.github/` (workflow, `.github/pr-gatekeeper.json`)
  or `AGENTS.md` in a PR. These paths are protected: touching them forces the
  decision to `HUMAN`.
- After 3 rounds that end in `CHANGES`, the next review is `HUMAN`. Make each
  round count: fix everything in the first pass if you can.

## GITHUB_TOKEN rule (Actions agents)

If you run inside GitHub Actions and push with the workflow's `GITHUB_TOKEN`,
that push does **not** trigger the pull_request workflow (GitHub's recursion
prevention). In that case you must end your run with:

```
gh workflow run pr-gatekeeper.yml -f pr=<N>
```

where `<N>` is the PR number. If you push with a PAT or another token that
does trigger workflows, this extra step is not needed — but running it is
always safe.

## Listening for the completion event

When a review finishes, pr-gatekeeper sends a `repository_dispatch` event of
type `gatekeeper-review-completed` with the client payload:

```json
{
  "pr": 123,
  "outcome": "CHANGES",
  "round": 2,
  "head_sha": "0123456789abcdef0123456789abcdef01234567"
}
```

Fields: `pr` (PR number), `outcome` (`MERGE`, `CHANGES`, or `HUMAN`), `round`
(review round number), `head_sha` (reviewed commit). To react to it, add a
workflow with:

```yaml
on:
  repository_dispatch:
    types: [gatekeeper-review-completed]
```

and read `github.event.client_payload.pr`, `.outcome`, `.round`, and
`.head_sha`. Use this to drive a follow-up agent run after each round without
polling comments.
<!-- pr-gatekeeper:end -->
