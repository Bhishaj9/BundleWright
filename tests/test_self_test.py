"""The CLI's own self-test is the contract; keep it green."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_self_test_passes():
    result = subprocess.run(
        [sys.executable, "-m", "bundlewright", "--self-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-2000:]
    assert "SELF-TEST PASSED" in output


def test_tool_version_reported():
    result = subprocess.run(
        [sys.executable, "-m", "bundlewright", "--tool-version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bundlewright 1.0.0"
