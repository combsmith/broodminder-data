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
Ref: combsmith/.github standards/github-settings.md#pr-quality--standard-ruleset-all-repositories
Ref: issue #29 (Scorecard: Branch-Protection 0/10 -> 8/10)
"""

from __future__ import annotations

import os
import re

import pytest

# Not a BroodMinder contract test; manages its own GitHub credential (or skips),
# so exempt from the BROODMINDER_API_KEY blanket skip.
pytestmark = pytest.mark.compliance

REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "combsmith/broodminder-data")
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


def _fnmatch_github_path(ref: str, pattern: str) -> bool:
    """Match a ref against a GitHub ruleset fnmatch pattern with path-aware semantics.

    In GitHub ruleset fnmatch syntax (Ruby File.fnmatch with File::FNM_PATHNAME):
    - '*' matches any character except '/'
    - '/**/' matches across path segments, including zero directories (e.g. '/**/' matches '/' or '/.+/')
    - '/**/*' matches recursively across nested path segments
    - bare '**' matches within a single path segment ('[^/]*') and cannot cross multiple '/' separators
    - '?' matches any single character except '/'
    - backslash escapes like '\\c' match literal 'c' both outside and inside character classes
    - character classes '[...]' match specified characters excluding '/' (FNM_PATHNAME restriction)
    """
    i = 0
    n = len(pattern)
    res: list[str] = []
    while i < n:
        if pattern[i:i + 4] == "/**/":
            while pattern[i:i + 4] == "/**/":
                i += 4
            while pattern[i:i + 3] == "**/":
                i += 3
            res.append("/(?:.+/)?")
            continue
        if i == 0 and pattern[i:i + 3] == "**/":
            while pattern[i:i + 3] == "**/":
                i += 3
            res.append("(?:.+/)?")
            continue
        if pattern[i:i + 2] == "**":
            res.append("[^/]*")
            i += 2
            continue
        c = pattern[i]
        if c == "\\":
            if i + 1 < n:
                res.append(re.escape(pattern[i + 1]))
                i += 2
            else:
                res.append(re.escape("\\"))
                i += 1
            continue
        elif c == "*":
            res.append("[^/]*")
            i += 1
        elif c == "?":
            res.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n:
                if pattern[j] == "\\":
                    j += 2
                    continue
                if pattern[j] == "]":
                    break
                j += 1
            if j >= n:
                res.append(re.escape(c))
                i += 1
            else:
                stuff = pattern[i + 1:j]
                i = j + 1
                if stuff.startswith("!") or stuff.startswith("^"):
                    is_negated = True
                    inner = stuff[1:]
                else:
                    is_negated = False
                    inner = stuff
                k = 0
                m = len(inner)
                inner_res = []
                while k < m:
                    ch = inner[k]
                    if ch == "\\":
                        if k + 1 < m:
                            next_ch = inner[k + 1]
                            if next_ch in ("]", "\\", "-", "^"):
                                inner_res.append("\\" + next_ch)
                            else:
                                inner_res.append(re.escape(next_ch))
                            k += 2
                            continue
                        else:
                            inner_res.append(r"\\")
                            k += 1
                            continue
                    if ch in ("]", "\\", "^"):
                        inner_res.append("\\" + ch)
                    else:
                        inner_res.append(ch)
                    k += 1
                class_body = "".join(inner_res)
                neg_prefix = "^" if is_negated else ""
                res.append(f"(?![/])[{neg_prefix}{class_body}]")
        else:
            res.append(re.escape(c))
            i += 1
    regex = "^" + "".join(res) + "$"
    return bool(re.match(regex, ref))


def _ref_matches_pattern(ref: str, pattern: str, default_branch: str = "main") -> bool:
    """Return whether ref matches GitHub ruleset ref pattern using path-aware fnmatch syntax."""
    if pattern == "~ALL":
        return True
    if pattern == "~DEFAULT_BRANCH":
        return ref in (default_branch, f"refs/heads/{default_branch}", "~DEFAULT_BRANCH")
    if _fnmatch_github_path(ref, pattern):
        return True
    if not pattern.startswith("refs/") and _fnmatch_github_path(ref, f"refs/heads/{pattern}"):
        return True
    if ref.startswith("refs/heads/") and _fnmatch_github_path(ref[len("refs/heads/"):], pattern):
        return True
    return False


def ruleset_targets_default_branch(ruleset_json: dict, default_branch: str = "main") -> bool:
    """Check whether a ruleset targets the default branch and does not exclude it.

    Fails closed:
    - Rejects payloads unless top-level target is 'branch'.
    - Rejects payloads if exclude patterns match the default branch.
    - Accepts payloads only if include patterns match the default branch.
    """
    if not isinstance(ruleset_json, dict):
        return False
    if str(ruleset_json.get("target", "")).lower() != "branch":
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

    target_refs = (f"refs/heads/{default_branch}", default_branch)
    if isinstance(exclude, list):
        for pattern in exclude:
            if isinstance(pattern, str) and any(
                _ref_matches_pattern(ref, pattern, default_branch=default_branch)
                for ref in target_refs
            ):
                return False
    for pattern in include:
        if isinstance(pattern, str) and any(
            _ref_matches_pattern(ref, pattern, default_branch=default_branch)
            for ref in target_refs
        ):
            return True
    return False


def evaluate_scorecard_tier1_compliance(
    rulesets_json: list[dict], default_branch: str = "main"
) -> tuple[bool, list[str]]:
    """Evaluate whether the given active rulesets satisfy OpenSSF Scorecard Tier 1.

    Scorecard Tier 1 mandates blocking deletions and non-fast-forward updates (force pushes).
    Returns (is_compliant, missing_rules).
    """
    if not isinstance(rulesets_json, list):
        return False, list(REQUIRED_RULE_TYPES)

    found_rules: set[str] = set()
    for rs in rulesets_json:
        if isinstance(rs, dict) and rs.get("enforcement", "").lower() == "active":
            if ruleset_targets_default_branch(rs, default_branch=default_branch):
                found_rules.update(extract_rule_types(rs))

    missing = [req for req in REQUIRED_RULE_TYPES if req not in found_rules]
    return len(missing) == 0, missing


# --- offline predicate tests (always run; give the guard teeth) --------------


def test_ruleset_has_rule_type_positive():
    """Verify ruleset_has_rule_type identifies present rules case-insensitively."""
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
    """Verify ruleset_has_rule_type returns False when rule types are missing."""
    ruleset = {
        "name": RULESET_NAME,
        "rules": [
            {"type": "pull_request", "parameters": {}},
        ],
    }
    assert ruleset_has_rule_type(ruleset, "deletion") is False
    assert ruleset_has_rule_type(ruleset, "non_fast_forward") is False


def test_ruleset_has_rule_type_invalid_payloads():
    """Verify ruleset_has_rule_type safely fails closed on invalid input types."""
    assert ruleset_has_rule_type({}, "deletion") is False
    assert ruleset_has_rule_type(None, "deletion") is False
    assert ruleset_has_rule_type({"rules": "invalid"}, "deletion") is False
    assert ruleset_has_rule_type({"rules": [None, 42, "string"]}, "deletion") is False


def test_extract_rule_types():
    """Verify extract_rule_types parses rule types and handles empty payloads."""
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
    """Verify ruleset_targets_default_branch identifies default branch targeting conditions."""
    rs_default = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    }
    rs_main = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    }
    rs_other = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/heads/feature/*"], "exclude": []}},
    }
    rs_all = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }
    assert ruleset_targets_default_branch(rs_default) is True
    assert ruleset_targets_default_branch(rs_main) is True
    assert ruleset_targets_default_branch(rs_all) is True
    assert ruleset_targets_default_branch(rs_other) is False
    assert ruleset_targets_default_branch({}) is False


def test_ruleset_targets_default_branch_rejects_non_branch_target():
    """Verify ruleset_targets_default_branch rejects rulesets targeting tags or missing target."""
    rs_tag = {
        "target": "tag",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }
    rs_push = {
        "target": "push",
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }
    rs_no_target = {
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
    }
    assert ruleset_targets_default_branch(rs_tag) is False
    assert ruleset_targets_default_branch(rs_push) is False
    assert ruleset_targets_default_branch(rs_no_target) is False


def test_ruleset_targets_default_branch_custom_default_branch():
    """Verify ruleset_targets_default_branch supports resolving custom repository default branches."""
    rs_trunk = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/heads/trunk"], "exclude": []}},
    }
    rs_keyword = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    }
    assert ruleset_targets_default_branch(rs_trunk, default_branch="trunk") is True
    assert ruleset_targets_default_branch(rs_trunk, default_branch="main") is False
    assert ruleset_targets_default_branch(rs_keyword, default_branch="trunk") is True


def test_ruleset_targets_default_branch_path_aware_fnmatch():
    """Verify single asterisk and bare trailing /** do not cross multiple path separators."""
    rs_single_star_cross_dir = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/*"], "exclude": []}},
    }
    rs_double_star_cross_dir = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/**"], "exclude": []}},
    }
    rs_recursive_wildcard = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/**/*"], "exclude": []}},
    }
    assert ruleset_targets_default_branch(rs_single_star_cross_dir) is False
    assert ruleset_targets_default_branch(rs_double_star_cross_dir) is False
    assert ruleset_targets_default_branch(rs_recursive_wildcard) is True


def test_ruleset_targets_default_branch_rejects_bracket_expression_matching_separator():
    """Verify character classes cannot match path separator / under FNM_PATHNAME."""
    rs_bracket_slash = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs[/]heads/main"], "exclude": []}},
    }
    rs_bracket_multi = {
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs[a-z/]heads/main"], "exclude": []}},
    }
    assert ruleset_targets_default_branch(rs_bracket_slash) is False
    assert ruleset_targets_default_branch(rs_bracket_multi) is False


def test_ruleset_targets_default_branch_rejects_excluded_main():
    """Verify ruleset_targets_default_branch rejects rulesets that explicitly exclude main."""
    rs_excluded_main = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/main"]}
        },
    }
    rs_excluded_default = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": ["~DEFAULT_BRANCH"]}
        },
    }
    assert ruleset_targets_default_branch(rs_excluded_main) is False
    assert ruleset_targets_default_branch(rs_excluded_default) is False


def test_ruleset_targets_default_branch_rejects_fnmatch_pattern_exclusions():
    """Verify ruleset_targets_default_branch rejects rulesets with fnmatch wildcard exclusions matching main."""
    rs_prefix_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/ma*"]}
        },
    }
    rs_short_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["ma*"]}
        },
    }
    rs_all_branches_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/*"]}
        },
    }
    assert ruleset_targets_default_branch(rs_prefix_wildcard) is False
    assert ruleset_targets_default_branch(rs_short_wildcard) is False
    assert ruleset_targets_default_branch(rs_all_branches_wildcard) is False


def test_ruleset_targets_default_branch_accepts_fnmatch_pattern_inclusions():
    """Verify ruleset_targets_default_branch accepts rulesets with fnmatch wildcard inclusions matching main."""
    rs_prefix_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["refs/heads/ma*"], "exclude": []}
        },
    }
    rs_all_branches_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["refs/heads/*"], "exclude": []}
        },
    }
    assert ruleset_targets_default_branch(rs_prefix_wildcard) is True
    assert ruleset_targets_default_branch(rs_all_branches_wildcard) is True


def test_ruleset_targets_default_branch_zero_directory_wildcard_exclusion():
    """Verify zero-directory matching for **/ properly excludes default branch."""
    rs_zero_dir_exclude = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/**/main"]}
        },
    }
    assert ruleset_targets_default_branch(rs_zero_dir_exclude) is False


def test_ruleset_targets_default_branch_zero_directory_wildcard_inclusion():
    """Verify zero-directory matching for **/ properly includes default branch."""
    rs_zero_dir_include = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["refs/heads/**/main"], "exclude": []}
        },
    }
    assert ruleset_targets_default_branch(rs_zero_dir_include) is True


def test_ruleset_targets_default_branch_consecutive_recursive_wildcards():
    """Verify consecutive **/ segments collapse into recursive directory matching."""
    rs_consecutive_wildcard = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/**/**/main"]}
        },
    }
    rs_consecutive_leading = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["**/**/main"], "exclude": []}
        },
    }
    assert ruleset_targets_default_branch(rs_consecutive_wildcard) is False
    assert ruleset_targets_default_branch(rs_consecutive_leading) is True


def test_ruleset_targets_default_branch_rejects_caret_negation_exclusion():
    """Verify caret negation [^...] in exclusion properly matches and excludes default branch."""
    rs_caret_exclude = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/m[^x]in"]}
        },
    }
    assert ruleset_targets_default_branch(rs_caret_exclude) is False


def test_ruleset_targets_default_branch_accepts_caret_negation_inclusion():
    """Verify caret negation [^...] in inclusion properly matches and includes default branch."""
    rs_caret_include = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["refs/heads/m[^x]in"], "exclude": []}
        },
    }
    assert ruleset_targets_default_branch(rs_caret_include) is True


def test_ruleset_targets_default_branch_rejects_backslash_escaped_exclusion():
    """Verify backslash-escaped pattern in exclusion properly matches and excludes default branch."""
    rs_escaped_exclude = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": [r"refs/heads/m\ain"]}
        },
    }
    assert ruleset_targets_default_branch(rs_escaped_exclude) is False


def test_ruleset_targets_default_branch_accepts_backslash_escaped_inclusion():
    """Verify backslash-escaped pattern in inclusion properly matches and includes default branch."""
    rs_escaped_include = {
        "target": "branch",
        "conditions": {
            "ref_name": {"include": [r"refs/heads/m\ain"], "exclude": []}
        },
    }
    assert ruleset_targets_default_branch(rs_escaped_include) is True


def test_ruleset_targets_default_branch_character_class_escapes():
    """Verify backslash escapes inside character classes are honored per Ruby fnmatch."""
    # Escaped letter inside class matches
    assert _fnmatch_github_path("refs/heads/main", r"refs/heads/m[\a]in") is True
    # Escaped hyphen prevents range interpretation
    assert _fnmatch_github_path("m-in", r"m[a\-c]in") is True
    assert _fnmatch_github_path("mbin", r"m[a\-c]in") is False
    # Escaped bracket inside class matches literal bracket
    assert _fnmatch_github_path("m]in", r"m[\]]in") is True
    # Escaped caret inside class is not negation
    assert _fnmatch_github_path("main", r"m[\^x]in") is False
    assert _fnmatch_github_path("m^in", r"m[\^x]in") is True



def test_evaluate_scorecard_tier1_compliance_multiple_same_named_rulesets():
    """Verify evaluation succeeds when an active ruleset satisfies protection despite an inactive duplicate."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "pull_request"},
            ],
        },
        {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "pull_request"},
                {"type": "deletion"},
                {"type": "non_fast_forward"},
            ],
        },
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is True
    assert missing == []


def test_evaluate_scorecard_tier1_compliance_layered_rulesets():
    """Verify evaluation succeeds when deletion and non_fast_forward are layered across multiple active rulesets."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
            ],
        },
        {
            "name": RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [
                {"type": "non_fast_forward"},
            ],
        },
    ]
    compliant, missing = evaluate_scorecard_tier1_compliance(rulesets)
    assert compliant is True
    assert missing == []


def test_evaluate_scorecard_tier1_compliance_success():
    """Verify evaluation succeeds when all required Tier 1 rules are present on active ruleset."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
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
    """Verify evaluation reports missing 'deletion' rule when omitted."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
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
    """Verify evaluation reports missing 'non_fast_forward' rule when omitted."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
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
    """Verify evaluation fails when ruleset enforcement is disabled."""
    rulesets = [
        {
            "name": RULESET_NAME,
            "target": "branch",
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
    """Verify evaluation handles empty ruleset listings gracefully."""
    compliant, missing = evaluate_scorecard_tier1_compliance([])
    assert compliant is False
    assert missing == ["deletion", "non_fast_forward"]


# --- live guard against the real ruleset (skips without a token) --------------


def _github_token() -> str | None:
    """Retrieve GitHub token from GH_TOKEN or GITHUB_TOKEN environment variables."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def test_branch_protection_tier1_rules_live():
    """Verify live GitHub ruleset defines active Tier 1 deletion and non_fast_forward rules."""
    token = _github_token()
    if not token:
        pytest.skip("no GH_TOKEN/GITHUB_TOKEN in env; live ruleset check skipped")

    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Resolve repository default branch
    repo_resp = httpx.get(
        f"https://api.github.com/repos/{REPO_SLUG}",
        headers=headers,
        timeout=15,
    )
    assert repo_resp.status_code == 200, (
        f"GitHub API returned HTTP {repo_resp.status_code} fetching {REPO_SLUG} metadata"
    )
    repo_meta = repo_resp.json()
    assert isinstance(repo_meta, dict), f"unexpected repo metadata format: {type(repo_meta).__name__}"
    default_branch = repo_meta.get("default_branch", "main")

    # Follow Link header pagination across all pages to collect all candidate rulesets matching RULESET_NAME
    matching_rulesets = []
    next_url: str | None = f"https://api.github.com/repos/{REPO_SLUG}/rulesets"
    request_params: dict = {"per_page": 100}

    while next_url is not None:
        listing = httpx.get(
            next_url,
            headers=headers,
            params=request_params,
            timeout=15,
        )
        assert listing.status_code == 200, (
            f"GitHub API returned HTTP {listing.status_code} listing {REPO_SLUG} rulesets"
        )

        listing_data = listing.json()
        assert isinstance(listing_data, list), (
            f"unexpected API response format for rulesets listing "
            f"(expected list, got {type(listing_data).__name__})"
        )

        for r in listing_data:
            if isinstance(r, dict) and r.get("name") == RULESET_NAME:
                matching_rulesets.append(r)

        # Advance to the next page (Link header)
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

    assert len(matching_rulesets) > 0, (
        f"repository must define or inherit the `{RULESET_NAME}` ruleset (org rulesets standard)"
    )

    # Fetch details for all same-named candidate rulesets
    candidate_details = []
    for rs in matching_rulesets:
        rs_id = rs.get("id")
        assert rs_id is not None, f"candidate ruleset '{RULESET_NAME}' missing 'id' field"
        detail = httpx.get(
            f"https://api.github.com/repos/{REPO_SLUG}/rulesets/{rs_id}",
            headers=headers,
            timeout=15,
        )
        assert detail.status_code == 200, (
            f"could not read `{RULESET_NAME}` ruleset detail for ID {rs_id} (HTTP {detail.status_code})"
        )
        detail_json = detail.json()
        assert isinstance(detail_json, dict), (
            f"unexpected detail response format: {type(detail_json).__name__}"
        )
        candidate_details.append(detail_json)

    # Evaluate all candidates to verify that active ruleset(s) protecting the default branch satisfy Tier 1 rules
    is_compliant, missing_rules = evaluate_scorecard_tier1_compliance(
        candidate_details, default_branch=default_branch
    )
    assert is_compliant, (
        f"Active `{RULESET_NAME}` ruleset(s) protecting '{default_branch}' must satisfy "
        f"OpenSSF Scorecard Branch-Protection Tier 1 requirements (#29). "
        f"Missing required rules: {missing_rules}. Evaluated {len(candidate_details)} candidate(s)."
    )
