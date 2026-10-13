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
    skipping gracefully when uncredentialed, and strictly failing on error when credentialed.

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
DEFAULT_BRANCH_TARGETS = ("~DEFAULT_BRANCH", "refs/heads/main", "main")


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
    """Check whether a ruleset targets the default branch and does not exclude it."""
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
    exclude = ref_name.get("exclude")
    if isinstance(exclude, list):
        if any(target in DEFAULT_BRANCH_TARGETS for target in exclude):
            return False
    return any(
        target in DEFAULT_BRANCH_TARGETS or target == "~ALL"
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
    rs_all = {
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}}
    }
    assert ruleset_targets_default_branch(rs_default) is True
    assert ruleset_targets_default_branch(rs_main) is True
    assert ruleset_targets_default_branch(rs_all) is True
    assert ruleset_targets_default_branch(rs_other) is False
    assert ruleset_targets_default_branch({}) is False


def test_ruleset_targets_default_branch_rejects_excluded_main():
    rs_excluded_main = {
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/main"]}
        }
    }
    rs_excluded_default = {
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["~DEFAULT_BRANCH"]}
        }
    }
    assert ruleset_targets_default_branch(rs_excluded_main) is False
    assert ruleset_targets_default_branch(rs_excluded_default) is False


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


def test_evaluate_scorecard_tier1_compliance_inactive_ruleset():
    rulesets = [
        {
            "name": RULESET_NAME,
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
            ],
        }
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is False
    assert missing == ["deletion", "non_fast_forward"]


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

    # Follow Link header pagination across all pages to locate RULESET_NAME
    matching_ruleset = None
    next_url: str | None = f"https://api.github.com/repos/{REPO_SLUG}/rulesets"
    request_params: dict = {"per_page": 100}

    while next_url is not None and matching_ruleset is None:
        try:
            listing = httpx.get(
                next_url,
                headers=headers,
                params=request_params,
                timeout=15,
            )
        except httpx.HTTPError as exc:
            pytest.fail(f"could not reach GitHub API listing rulesets: {exc}")

        if listing.status_code != 200:
            pytest.fail(
                f"GitHub API returned HTTP {listing.status_code} listing {REPO_SLUG} rulesets"
            )

        try:
            listing_data = listing.json()
        except Exception as exc:
            pytest.fail(f"could not parse ruleset listing JSON: {exc}")

        if not isinstance(listing_data, list):
            pytest.fail(
                f"unexpected API response format for rulesets listing "
                f"(expected list, got {type(listing_data).__name__})"
            )

        matching_ruleset = next(
            (r for r in listing_data if isinstance(r, dict) and r.get("name") == RULESET_NAME),
            None,
        )

        # Advance to the next page (Link header) unless the ruleset was already found.
        link_header = listing.headers.get("Link", "")
        next_url = None
        request_params = {}
        for part in link_header.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                url_part = part.split(";")[0].strip()
                if url_part.startswith("<") and url_part.endswith(">"):
                    next_url = url_part[1:-1]
                    break

    assert matching_ruleset is not None, (
        f"repository must define the `{RULESET_NAME}` ruleset (org rulesets standard)"
    )

    ruleset_id = matching_ruleset.get("id")
    if ruleset_id is None:
        pytest.fail(f"ruleset '{RULESET_NAME}' found but is missing an 'id' field")

    try:
        detail = httpx.get(
            f"https://api.github.com/repos/{REPO_SLUG}/rulesets/{ruleset_id}",
            headers=headers,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        pytest.fail(f"could not reach GitHub API fetching ruleset detail: {exc}")

    if detail.status_code != 200:
        pytest.fail(
            f"could not read `{RULESET_NAME}` ruleset detail (HTTP {detail.status_code})"
        )

    try:
        detail_json = detail.json()
    except Exception as exc:
        pytest.fail(f"could not parse ruleset detail JSON: {exc}")

    # Validate active enforcement and branch applicability
    assert detail_json.get("enforcement", "").lower() == "active", (
        f"`{RULESET_NAME}` ruleset enforcement must be 'active', got {detail_json.get('enforcement')!r}"
    )
    assert ruleset_targets_default_branch(detail_json) is True, (
        f"`{RULESET_NAME}` ruleset must target default branch without excluding it"
    )

    # Validate required Tier 1 rules
    for rule_type in REQUIRED_RULE_TYPES:
        assert ruleset_has_rule_type(detail_json, rule_type) is True, (
            f"Rule `{rule_type}` must be enabled on the `{RULESET_NAME}` ruleset to satisfy "
            f"OpenSSF Scorecard Branch-Protection Tier 1 requirements (#29)."
        )
