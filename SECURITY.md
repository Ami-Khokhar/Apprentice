# Security policy

## Supported versions

Apprentice is pre-release software. Security fixes are made on the `main` branch
and included in the next release. Older snapshots and forks are not maintained
by this project.

## Deployment boundary

Apprentice is designed for one user on a trusted local machine. Run it on
`127.0.0.1` and do not expose it to a LAN, the public internet, a reverse proxy,
or untrusted users. It is not an authenticated multi-user service.

The application stores learner data in an unencrypted local SQLite database.
Model-assisted operations send relevant professional context, learner responses,
scenario state, evidence, and prompts to OpenAI through the locally authenticated
Codex CLI. Optional Langfuse tracing sends trace data to the configured Langfuse
service. See the README's **Data and privacy** section before using real
professional information.

## Report a vulnerability

Please report suspected vulnerabilities privately through this repository's
GitHub Security Advisory flow:

1. Open the repository's **Security** tab.
2. Select **Advisories** and then **Report a vulnerability**.
3. Include the affected version or commit, impact, reproduction steps, and any
   suggested mitigation.

Do not disclose the issue in a public GitHub issue, pull request, discussion, or
social post before a fix is available. Do not include real learner data,
credentials, or other people's private information in a report; use synthetic
examples.

The maintainers will acknowledge the report through the private advisory,
investigate it, and coordinate disclosure and credit with the reporter. Response
and remediation times depend on severity and maintainer availability; this
pre-release project does not currently offer a service-level commitment or bug
bounty.

If GitHub does not show **Report a vulnerability**, use GitHub to ask the
repository maintainers to enable private vulnerability reporting without
sharing vulnerability details publicly.

## Security research

Keep testing within installations and accounts you own or have explicit
permission to test. Do not access, alter, retain, or disclose another person's
data, disrupt services, or incur model usage on another person's account.
