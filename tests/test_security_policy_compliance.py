"""Compliance test for OpenSSF Scorecard Security-Policy check (#33).

Validates that no local `SECURITY.md` shadows the canonical org-wide policy in
`combsmith/.github/SECURITY.md` (which both GitHub and OpenSSF Scorecard inherit
automatically to score 10/10), and that `README.md` links directly to the
org-wide policy.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_local_security_md_shadows_org_policy() -> None:
    """Ensure no local SECURITY.md shadows combsmith/.github/SECURITY.md."""
    assert not (ROOT / "SECURITY.md").exists()
    assert not (ROOT / ".github" / "SECURITY.md").exists()
    assert not (ROOT / "docs" / "SECURITY.md").exists()


def test_readme_links_to_org_security_policy() -> None:
    """Ensure README.md links to the canonical org-wide SECURITY.md."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://github.com/combsmith/.github/blob/main/SECURITY.md" in readme
    assert "(SECURITY.md)" not in readme
