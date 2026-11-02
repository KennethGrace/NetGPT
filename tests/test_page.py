"""
Tests for the page's own logic, in JavaScript: the registry's names and tones
(tests/page/registry_test.js), run with Node's test runner. Skipped where Node
isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_TESTS = Path(__file__).parent / "page"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node isn't installed")
def test_the_page_s_registry_names_and_colors_what_the_conversation_mentions() -> None:
    finished = subprocess.run(
        ["node", "--test", *sorted(str(path) for path in PAGE_TESTS.glob("*_test.js"))],
        capture_output=True, text=True, timeout=120,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr
