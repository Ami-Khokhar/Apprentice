# Contributing to Apprentice

Thank you for helping improve Apprentice. Contributions should preserve its core
product promise: realistic practice, deterministic consequences, and
evidence-grounded reflection.

## Before opening a change

- Search existing issues and pull requests to avoid duplicate work.
- For a substantial feature or behavior change, open an issue first so the
  problem, scope, and product tradeoffs can be discussed.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Never commit learner data, credentials, local databases, trace files, or
  proprietary professional information. Use synthetic fixtures.

## Development setup

Requirements:

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

Install the locked development environment:

```bash
uv sync --locked
```

The test suite uses deterministic fake model responses and does not require a
Codex login, network access, or model credentials.

## Making a change

Keep changes narrow and consistent with the surrounding code. Add or update
tests for behavior changes. Do not make unrelated formatting or refactoring
changes in the same pull request.

Run the same quality gates as continuous integration:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

For user-interface changes, describe the browsers and viewport sizes checked and
include screenshots when they materially help review. Screenshots and fixtures
must contain only synthetic data.

## Pull requests

A pull request should:

- explain the user-facing problem and the chosen solution;
- identify important assumptions or tradeoffs;
- include tests or explain why no test is needed;
- document new configuration, data flows, or privacy implications; and
- pass all automated checks.

Maintainers may ask for a smaller change if a pull request combines unrelated
concerns.

## Licensing

By intentionally submitting a contribution for inclusion in Apprentice, you
agree that it is provided under the
[Apache License 2.0](LICENSE), as described in section 5 of that license. Only
submit work that you have the right to contribute.

Document the source, creator, license, and required attribution for any new
non-code asset in [ASSET_PROVENANCE.md](ASSET_PROVENANCE.md).

Participation in this project is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
