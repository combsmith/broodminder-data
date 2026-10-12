"""Compliance test for OpenSSF Scorecard Security-Policy check (#33).

Validates that `SECURITY.md` exists and satisfies OpenSSF Scorecard's
four Security-Policy evaluation probes:
1. `securityPolicyPresent`: file exists at the repository root.
2. `securityPolicyContainsLinks`: contains valid URL (https://) and/or email contacts.
3. `securityPolicyContainsText`: contains substantive explanatory text beyond raw links.
4. `securityPolicyContainsVulnerabilityDisclosure`: contains hints for disclosure and
   vulnerabilities ("Vuln", "Disclos", and timeline numbers) with count > 1.

Ref: https://github.com/ossf/scorecard/blob/main/checks/evaluation/security_policy.go
Ref: https://github.com/ossf/scorecard/blob/main/checks/raw/security_policy.go
Ref: issue #33 (Scorecard: Security-Policy 9/10 -> 10/10)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SECURITY_FILE = ROOT / "SECURITY.md"

# OpenSSF Scorecard regex patterns from checks/raw/security_policy.go
RE_URL = re.compile(r"(http|https)://[a-zA-Z0-9./?=_%:-]*")
RE_EML = re.compile(r"\b[A-Za-z0-9._%+-]+(@|\\?\[at\\?\])[A-Za-z0-9.-]+\.[A-Za-z]{2,6}\b")
RE_DIG_DISC_VULN = re.compile(r"(?i)(\b[0-9]{1,4}\b|(Disclos|Vuln))")


def _security_content() -> str:
    assert SECURITY_FILE.exists(), f"{SECURITY_FILE} must exist at repository root"
    return SECURITY_FILE.read_text(encoding="utf-8")


def test_security_policy_file_present():
    """Probe: securityPolicyPresent.

    Ensures SECURITY.md is located at the root directory and non-empty.
    """
    assert SECURITY_FILE.is_file(), "SECURITY.md must be a regular file"
    content = _security_content().strip()
    assert len(content) > 0, "SECURITY.md must not be empty"


def test_security_policy_contains_links_and_emails():
    """Probe: securityPolicyContainsLinks.

    Scorecard awards 6 points if the security policy contains web links
    or contact email addresses for reporting vulnerabilities.
    """
    content = _security_content()
    urls = RE_URL.findall(content)
    emails = RE_EML.findall(content)

    assert len(urls) > 0, "SECURITY.md must contain at least one HTTP/HTTPS URL"
    assert len(emails) > 0, "SECURITY.md must contain at least one contact email address"


def test_security_policy_contains_sufficient_text():
    """Probe: securityPolicyContainsText.

    Scorecard awards 3 points if the security policy contains descriptive,
    explanatory text rather than merely bare links.
    """
    content = _security_content()
    # Remove matched URLs and emails to ensure substantive body text remains
    text_only = RE_URL.sub("", content)
    text_only = RE_EML.sub("", text_only).strip()

    # Scorecard expects descriptive guidance
    assert len(text_only) >= 200, "SECURITY.md must contain substantial explanatory text"


def test_security_policy_contains_vulnerability_disclosure_hints():
    """Probe: securityPolicyContainsVulnerabilityDisclosure.

    Scorecard awards 1 point when secpolicy.CountSecInfo(...) > 1 for text matches
    against `(?i)(\b*[0-9]{1,4}\b|(Disclos|Vuln))`. Both disclosure/vulnerability
    terminology and response timelines must be present.
    """
    content = _security_content()
    matches = RE_DIG_DISC_VULN.findall(content)
    assert len(matches) > 1, (
        f"Scorecard requires more than 1 vulnerability/disclosure hit; got {len(matches)}"
    )

    # Confirm both 'vuln' and 'disclos' families are represented
    lower = content.lower()
    assert "vuln" in lower, "SECURITY.md must mention vulnerability / vulnerabilities"
    assert "disclos" in lower, "SECURITY.md must mention disclosure / disclose"


def test_security_policy_private_reporting_guidance():
    """Ensures security policy explicitly warns against opening public issues
    and directs reporters to private reporting mechanisms."""
    content = _security_content()
    lower = content.lower()
    assert "do not open a public issue" in lower or "do not open public issues" in lower
    assert "private" in lower
    assert "advisories" in lower or "report a vulnerability" in lower
