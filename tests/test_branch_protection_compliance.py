"""Compliance test for OpenSSF Scorecard Branch-Protection check (#29).

The OpenSSF Scorecard Branch-Protection check evaluates whether a project's default
and release branches are protected with GitHub branch protection or repository rulesets.
Scorecard uses tiered scoring where Tier 1 (3/10) requires:
1. Prevent branch deletion (`deletion` rule in GitHub Rulesets)
2. Prevent force pushes (`non_fast_forward` rule in GitHub Rulesets)

If Tier 1 fails, Scorecard awards 0/10 points regardless of higher-tier protections
(such as required reviews or status checks).

Rulesets live in the GitHub control plane, not in a file inside this repo, so mirroring
``test_ruleset_compliance.py`` and ``test_pr_quality_ruleset_compliance.py``, this guard:
  * unit-tests the compliance predicates offline (always runs, fast, deterministic), and
  * verifies the *live* ruleset via the GitHub REST API when a token is present,
    skipping gracefully otherwise (green out of the box, real check when credentialed).

Ref: https://github.com/ossf/scorecard/blob/main/docs/checks.md#branch-protection
Ref: petry-projects/.github standards/github-settings.md#pr-quality--standard-ruleset-all-repositories
Ref: issue #29 (Scorecard: Branch-Protection 0/10 -> 8/10)
"""

from __future__ import annotations

import os

import pytest

# Not a BroodMinder contract test; manages its own GitHub credential (or skips),
# so exempt from the BROODMINDER_API_KEY blanket skip.
pytestmark = pytest.mark.compliance

REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "petry-projects/broodminder-data")
RULESET_NAME = "pr-quality"
REQUIRED_RULE_TYPES = ("deletion", "non_fast_forward")


def ruleset_has_rule_type(ruleset_json: dict, rule_type: str) -> bool:
    """Return whether ``ruleset_json`` defines a rule of type ``rule_type``.

    Fails closed: returns False if ruleset_json is not a dict or rules is not a list.
    """
    if not isinstance(ruleset_json, dict):
        return False
    rules = ruleset_json.get("rules")
    if not isinstance(rules, list):
        return False
    return any(
        isinstance(r, dict) and r.get("type", "").lower() == rule_type.lower()
        for r in rules
    )


def extract_rule_types(ruleset_json: dict) -> set[str]:
    """Extract set of rule types from a ruleset dictionary."""
    if not isinstance(ruleset_json, dict):
        return set()
    rules = ruleset_json.get("rules")
    if not isinstance(rules, list):
        return set()
    return {
        r.get("type", "").lower()
        for r in rules
        if isinstance(r, dict) and "type" in r
    }


def ruleset_targets_default_branch(ruleset_json: dict) -> bool:
    """Check whether a ruleset targets the default branch or all branches."""
    if not isinstance(ruleset_json, dict):
        return False
    conditions = ruleset_json.get("conditions")
    if not isinstance(conditions, dict):
        return False
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict):
        return False
    include = ref_name.get("include")
    if not isinstance(include, list):
        return False
    return any(
        target in ("~DEFAULT_BRANCH", "refs/heads/main", "main", "~ALL")
        for target in include
    )


def evaluate_scorecard_tier1_compliance(rulesets_json: list[dict]) -> tuple[bool, list[str]]:
    """Evaluate whether the given active rulesets satisfy OpenSSF Scorecard Tier 1.

    Scorecard Tier 1 mandates blocking deletions and non-fast-forward updates (force pushes).
    Returns (is_compliant, missing_rules).
    """
    if not isinstance(rulesets_json, list):
        return False, list(REQUIRED_RULE_TYPES)

    found_rules: set[str] = set()
    for rs in rulesets_json:
        if isinstance(rs, dict) and rs.get("enforcement", "").lower() == "active":
            if ruleset_targets_default_branch(rs):
                found_rules.update(extract_rule_types(rs))

    missing = [req for req in REQUIRED_RULE_TYPES if req not in found_rules]
    return len(missing) == 0, missing


# --- offline predicate tests (always run; give the guard teeth) --------------


def test_ruleset_has_rule_type_positive():
    ruleset = {
        "name": RULESET_NAME,
        "rules": [
            {"type": "pull_request", "parameters": {}},
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    }
    assert ruleset_has_rule_type(ruleset, "deletion") is True
    assert ruleset_has_rule_type(ruleset, "non_fast_forward") is True
    assert ruleset_has_rule_type(ruleset, "NON_FAST_FORWARD") is True


def test_ruleset_has_rule_type_negative():
    ruleset = {
        "name": RULESET_NAME,
        "rules": [
            {"type": "pull_request", "parameters": {}},
        ],
    }
    assert ruleset_has_rule_type(ruleset, "deletion") is False
    assert ruleset_has_rule_type(ruleset, "non_fast_forward") is False


def test_ruleset_has_rule_type_invalid_payloads():
    assert ruleset_has_rule_type({}, "deletion") is False
    assert ruleset_has_rule_type(None, "deletion") is False
    assert ruleset_has_rule_type({"rules": "invalid"}, "deletion") is False
    assert ruleset_has_rule_type({"rules": [None, 42, "string"]}, "deletion") is False


def test_extract_rule_types():
    ruleset = {
        "rules": [
            {"type": "pull_request"},
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ]
    }
    assert extract_rule_types(ruleset) == {"pull_request", "deletion", "non_fast_forward"}
    assert extract_rule_types({}) == set()
    assert extract_rule_types(None) == set()


def test_ruleset_targets_default_branch():
    rs_default = {
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
    }
    rs_main = {
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}
    }
    rs_other = {
        "conditions": {"ref_name": {"include": ["refs/heads/feature/*"], "exclude": []}}
    }
    assert ruleset_targets_default_branch(rs_default) is True
    assert ruleset_targets_default_branch(rs_main) is True
    assert ruleset_targets_default_branch(rs_other) is False
    assert ruleset_targets_default_branch({}) is False


def test_evaluate_scorecard_tier1_compliance_success():
    rulesets = [
        {
            "name": RULESET_NAME,
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "pull_request"},
                {"type": "deletion"},
                {"type": "non_fast_forward"},
            ],
        }
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is True
    assert missing == []


def test_evaluate_scorecard_tier1_compliance_missing_deletion():
    rulesets = [
        {
            "name": RULESET_NAME,
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "pull_request"},
                {"type": "non_fast_forward"},
            ],
        }
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is False
    assert missing == ["deletion"]


def test_evaluate_scorecard_tier1_compliance_missing_non_fast_forward():
    rulesets = [
        {
            "name": RULESET_NAME,
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "pull_request"},
                {"type": "deletion"},
            ],
        }
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is False
    assert missing == ["non_fast_forward"]


def test_evaluate_scorecard_tier1_compliance_empty():
    compliant, missing = evaluate_scorecard_tier1_compliance([])
    assert compliant is False
    assert missing == ["deletion", "non_fast_forward"]


# --- live guard against the real ruleset (skips without a token) --------------


def _github_token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def test_branch_protection_tier1_rules_live():
    token = _github_token()
    if not token:
        pytest.skip("no GH_TOKEN/GITHUB_TOKEN in env; live ruleset check skipped")

    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        listing = httpx.get(
            f"https://api.github.com/repos/{REPO_SLUG}/rulesets",
            headers=headers,
            params={"per_page": 100},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        pytest.skip(f"could not reach GitHub API: {exc}")

    if listing.status_code != 200:
        if listing.status_code in (401, 403):
            pytest.fail(
                f"access denied listing {REPO_SLUG} rulesets "
                f"(HTTP {listing.status_code}); "
                "ensure GH_TOKEN has 'Metadata' (read) repository permission"
            )
        pytest.skip(
            f"could not list {REPO_SLUG} rulesets (HTTP {listing.status_code}); "
            "live ruleset check skipped"
        )

    rulesets = listing.json()
    if not isinstance(rulesets, list):
        pytest.skip(f"unexpected API response format: {type(rulesets).__name__}")

    match = next(
        (r for r in rulesets if isinstance(r, dict) and r.get("name") == RULESET_NAME),
        None,
    )
    assert match is not None, (
        f"repository must define the `{RULESET_NAME}` ruleset (org rulesets standard)"
    )

    ruleset_id = match.get("id")
    if ruleset_id is None:
        pytest.skip(f"ruleset '{RULESET_NAME}' found but is missing an 'id' field")

    try:
        detail = httpx.get(
            f"https://api.github.com/repos/{REPO_SLUG}/rulesets/{ruleset_id}",
            headers=headers,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        pytest.skip(f"could not reach GitHub API: {exc}")

    if detail.status_code != 200:
        if detail.status_code in (401, 403):
            pytest.fail(
                f"access denied reading '{RULESET_NAME}' ruleset (HTTP {detail.status_code})"
            )
        pytest.skip(
            f"could not read `{RULESET_NAME}` ruleset (HTTP {detail.status_code})"
        )

    detail_json = detail.json()
    for rule_type in REQUIRED_RULE_TYPES:
        assert ruleset_has_rule_type(detail_json, rule_type) is True, (
            f"Rule `{rule_type}` must be enabled on the `{RULESET_NAME}` ruleset to satisfy "
            f"OpenSSF Scorecard Branch-Protection Tier 1 requirements (#29)."
        )
