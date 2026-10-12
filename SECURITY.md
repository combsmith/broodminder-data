# Security Policy

This policy applies to `broodminder-data` and aligns with the org-wide policy in
[`petry-projects/.github`](https://github.com/petry-projects/.github/blob/main/SECURITY.md).

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via **GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**:
open [petry-projects/broodminder-data](https://github.com/petry-projects/broodminder-data) → **Security** tab → **[Report a vulnerability](https://github.com/petry-projects/broodminder-data/security/advisories/new)**.

If private vulnerability reporting is unavailable, email **<donpetry@gmail.com>**
with the details and "SECURITY" in the subject.

Please include, where possible:

- The affected component, CLI command, SDK method, or workflow.
- BroodMinder API client version or commit hash.
- A clear description of the vulnerability and its potential security impact.
- Step-by-step instructions or proof-of-concept to reproduce the behavior.
- Any suggested remediation or patch.

## Vulnerability Disclosure Process & Timelines

We follow coordinated vulnerability disclosure:

- **Acknowledgement:** We will acknowledge receipt of your report within 5 business days.
- **Assessment:** We will assess the severity and impact, verifying the report in an isolated test environment.
- **Remediation:** For confirmed issues, we will develop a remediation plan with a target patch timeline proportional to severity (typically within 30 to 90 days).
- **Public Disclosure:** We will coordinate public disclosure alongside a new release and advisory credit.

## Supported Versions

These are actively-maintained projects; security fixes are applied to the
**default branch** (`main`) and the current release line. Older tags and historical releases
are not patched — please upgrade to the latest release (`pip install -U broodminder-data`).

## Scope

In scope:
- Python client SDK (`bm/client.py`, `bm/battery.py`).
- CLI tools (`scripts/`).
- GitHub Actions workflows and automation (`.github/workflows/`).
- OpenAPI 3.1 specification (`openapi/broodminder-openapi.yaml`).

Out of scope:
- The external BroodMinder API itself (report directly to BroodMinder support at support@broodminder.com).
- Third-party Python dependencies (report upstream).
- Attack scenarios requiring compromised maintainer credentials or physical machine access.
