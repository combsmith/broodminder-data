"""Shell script hygiene guards (SonarCloud shelldre:S7688)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
BASH_SCRIPTS = sorted(SCRIPTS_DIR.glob("*.sh"))

# A single-bracket test: `[` not preceded/followed by another `[`, then whitespace.
SINGLE_BRACKET_TEST = re.compile(r"(?<![\[\w])\[(?!\[)\s")


@pytest.mark.parametrize("script", BASH_SCRIPTS, ids=lambda p: p.name)
def test_bash_scripts_use_double_bracket_conditionals(script):
    offenders = [
        f"{script.name}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(script.read_text().splitlines(), start=1)
        if not line.lstrip().startswith("#") and SINGLE_BRACKET_TEST.search(line)
    ]
    assert not offenders, "Use '[[' instead of '[' for conditional tests:\n" + "\n".join(offenders)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("script", BASH_SCRIPTS, ids=lambda p: p.name)
def test_bash_scripts_parse(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
