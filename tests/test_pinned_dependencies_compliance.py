"""Compliance test for OpenSSF Scorecard Pinned-Dependencies check (#44).

Validates that dependency manifests and GitHub Actions workflows satisfy
OpenSSF Scorecard's Pinned-Dependencies evaluation probes and org standards:
1. `requirements.txt` exists and is hash-pinned (every dependency has an exact
   `==` pin and at least one `--hash=sha256:` entry).
2. `scripts/build-requirements.txt` exists, contains `build` and `twine`, and is
   hash-pinned.
3. Every `pip install` invocation across active GitHub Actions workflows
   (`ci.yml`, `sonarcloud.yml`, `publish.yml`) enforces `--require-hashes` and
   `--only-binary :all:`.
4. Workflows contain zero unpinned pip install commands, ensuring OpenSSF
   Scorecard evaluates zero `pipCommand not pinned by hash` warnings and
   satisfies the first-party channel tag accepted exception (#709).

Ref: https://github.com/ossf/scorecard/blob/main/checks/raw/shell_download_validate.go
Ref: petry-projects/.github standards/ci-standards.md#openssf-scorecard-org-scorecardyml
Ref: issue #44 (Scorecard: Pinned-Dependencies 2/10)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = ROOT / "requirements.txt"
BUILD_REQUIREMENTS_FILE = ROOT / "scripts" / "build-requirements.txt"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

_PIN_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[0-9A-Za-z._+-]+)"
    r"(?:;\s*(?P<marker>[^#\\\n]+?))?\s*(?:\\)?$"
)
_HASH_OPTION = re.compile(r"^--hash=sha256:[0-9a-f]{64}\s*(?:\\)?$", re.IGNORECASE)


def validate_require_hashes_file(path: Path, expected_packages: list[str] | None = None) -> list[str]:
    """Return violations if ``path`` is not a valid ``--require-hashes`` requirements file."""
    if not path.exists():
        return [f"{path.name} is missing"]

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"{path.name} is empty"]

    blocks: list[tuple[str, list[str]]] = []
    violations: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_LINE.match(line)
        if m:
            blocks.append((m.group("name"), [line]))
        elif _HASH_OPTION.match(line):
            if blocks:
                blocks[-1][1].append(line)
            else:
                violations.append(f"hash line without package: {line!r}")
        else:
            violations.append(f"unpinned or invalid line in {path.name}: {line!r}")

    if not blocks:
        violations.append(f"no pinned requirements found in {path.name}")

    norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
    parsed_names = {norm(name) for name, _ in blocks}

    if expected_packages:
        for pkg in expected_packages:
            if norm(pkg) not in parsed_names:
                violations.append(f"missing expected package {pkg!r} in {path.name}")

    for name, lines in blocks:
        hashes = [ln for ln in lines[1:] if _HASH_OPTION.match(ln)]
        if not hashes:
            violations.append(f"requirement {name!r} in {path.name} is missing a --hash=sha256 entry")

    return violations


def test_requirements_file_exists_and_is_hash_pinned():
    """Validates that root requirements.txt is fully hash-pinned for CI and runtime,
    including conditional dependencies for Python <3.11 and Windows environments."""
    violations = validate_require_hashes_file(
        REQUIREMENTS_FILE,
        expected_packages=[
            "httpx",
            "python-dotenv",
            "pytest",
            "pytest-cov",
            "exceptiongroup",
            "tomli",
            "colorama",
        ],
    )
    assert not violations, f"requirements.txt has compliance violations: {violations}"


def test_build_requirements_file_exists_and_is_hash_pinned():
    """Validates that scripts/build-requirements.txt exists and is hash-pinned."""
    violations = validate_require_hashes_file(
        BUILD_REQUIREMENTS_FILE,
        expected_packages=["build", "twine", "setuptools", "wheel"],
    )
    assert not violations, f"scripts/build-requirements.txt has violations: {violations}"


def _get_active_workflow_pip_lines() -> list[tuple[str, str]]:
    """Collect all non-comment pip install command lines in active workflows."""
    results: list[tuple[str, str]] = []
    target_workflows = ["ci.yml", "sonarcloud.yml", "publish.yml"]

    for name in target_workflows:
        wf = WORKFLOWS_DIR / name
        assert wf.exists(), f"Workflow {name} must exist"
        for line in wf.read_text(encoding="utf-8").splitlines():
            trimmed = line.strip()
            if trimmed.startswith("#"):
                continue
            if trimmed.startswith("echo"):
                continue
            if re.search(r"\b(pip|pip3)\s+install\b", trimmed):
                results.append((name, trimmed))
    return results


def test_workflow_pip_installs_enforce_require_hashes_and_only_binary():
    """Ensures every pip install in active workflows has --require-hashes and --only-binary :all:."""
    pip_lines = _get_active_workflow_pip_lines()
    assert pip_lines, "Expected to find pip install commands in workflows"

    for wf_name, cmd in pip_lines:
        assert "--require-hashes" in cmd, (
            f"{wf_name} command missing --require-hashes: {cmd}"
        )
        assert "--only-binary :all:" in cmd, (
            f"{wf_name} command missing --only-binary :all:: {cmd}"
        )


def test_scorecard_pip_download_predicate_satisfaction():
    """Simulates OpenSSF Scorecard's `isUnpinnedPipInstall` logic.

    Scorecard inspects shell tokens. When `--require-hashes` is present in the command
    arguments, Scorecard considers the pip install command securely pinned.
    """
    pip_lines = _get_active_workflow_pip_lines()

    for wf_name, cmd in pip_lines:
        tokens = cmd.split()
        assert "--require-hashes" in tokens, (
            f"Scorecard token check: {wf_name} command missing --require-hashes token: {cmd}"
        )


def test_regeneration_comments_have_valid_syntax():
    """Ensures regeneration instructions in requirements files do not contain accidental '#' comment markers."""
    for path in [REQUIREMENTS_FILE, BUILD_REQUIREMENTS_FILE]:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines()[:15]:
            if "pip-compile" in line:
                # Should not have another '#' in the middle of the pip-compile command
                cmd_part = line.lstrip("#").strip()
                assert not cmd_part.startswith("pip-compile") or "#" not in cmd_part, (
                    f"Found stray '#' in pip-compile regeneration command in {path.name}: {line}"
                )


def test_validation_rejects_comment_only_hashes_and_invalid_lines(tmp_path: Path):
    """Ensures requirements with hashes only in comments or invalid syntax are rejected."""
    comment_hash_file = tmp_path / "comment_hash.txt"
    comment_hash_file.write_text(
        "pkg==1.0 # --hash=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    violations = validate_require_hashes_file(comment_hash_file)
    assert any("unpinned or invalid line" in v for v in violations)

    continuation_comment_file = tmp_path / "cont_comment.txt"
    continuation_comment_file.write_text(
        "pkg==1.0 \\\n  # --hash=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        encoding="utf-8",
    )
    violations = validate_require_hashes_file(continuation_comment_file)
    assert any("missing a --hash=sha256 entry" in v for v in violations)

    trailing_junk_file = tmp_path / "trailing_junk.txt"
    trailing_junk_file.write_text(
        "pkg==1.0 \\\n  --hash=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef invalid_junk\n",
        encoding="utf-8",
    )
    violations = validate_require_hashes_file(trailing_junk_file)
    assert any("unpinned or invalid line" in v for v in violations)


