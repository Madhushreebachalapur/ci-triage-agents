"""
Isolated tests for _apply_diff() in orchestrator/ci_triage.py.

Strategy: create real temporary .py files inside src/ (the only permitted
directory), build diff strings targeting them by relative path, run
_apply_diff(), assert outcomes, then always clean up.

We import _apply_diff and the two module-level constants (REPO_ROOT, SRC_DIR)
directly from the orchestrator module so we test the live implementation with
no monkey-patching.
"""

import importlib
import os
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the module under test.
# ci_triage.py prints "OFFLINE MODE" at import time — that is expected and
# harmless here; we capture it so test output stays clean.
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _import_apply_diff():
    """Return the _apply_diff callable from orchestrator.ci_triage."""
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        import orchestrator.ci_triage as _m
    return _m._apply_diff, _m.REPO_ROOT, _m.SRC_DIR


_apply_diff, REPO_ROOT, SRC_DIR = _import_apply_diff()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_src_file(name: str, content: str) -> Path:
    """Write content to src/<name> and return the Path."""
    p = SRC_DIR / name
    p.write_text(content, encoding="utf-8")
    return p


def _unified_diff_str(rel_path: str, old_lines: list[str], new_lines: list[str],
                      context: int = 3) -> str:
    """Build a minimal unified diff string targeting a/rel_path → b/rel_path."""
    import difflib
    return "".join(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=context,
        )
    )


# ---------------------------------------------------------------------------
# 1. Valid single-line patch — applies correctly and returns True
# ---------------------------------------------------------------------------

def test_valid_single_line_patch_applies_and_returns_true(tmp_path):
    fname = "_triage_test_valid.py"
    original = "x = 1\n"
    patched  = "x = 2\n"
    target = _make_src_file(fname, original)
    try:
        diff = _unified_diff_str(f"src/{fname}", [original], [patched])
        result = _apply_diff(diff)
        assert result is True, "Expected True for a valid single-line patch"
        assert target.read_text(encoding="utf-8") == patched, \
            "File content should be updated to patched value"
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Content before and after changed line is fully preserved
# ---------------------------------------------------------------------------

def test_surrounding_content_preserved(tmp_path):
    fname = "_triage_test_context.py"
    lines_orig = [
        "# header\n",
        "def foo():\n",
        "    return 'old'\n",   # line 3 — will be changed
        "\n",
        "# footer\n",
    ]
    lines_new = lines_orig.copy()
    lines_new[2] = "    return 'new'\n"

    target = _make_src_file(fname, "".join(lines_orig))
    try:
        diff = _unified_diff_str(f"src/{fname}", lines_orig, lines_new)
        result = _apply_diff(diff)
        assert result is True
        got = target.read_text(encoding="utf-8").splitlines(keepends=True)
        assert got == lines_new, \
            f"Full file should match patched lines; got {got!r}"
        # Explicitly verify unchanged lines
        assert got[0] == "# header\n"
        assert got[1] == "def foo():\n"
        assert got[3] == "\n"
        assert got[4] == "# footer\n"
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Context mismatch — patch rejected, original file untouched
# ---------------------------------------------------------------------------

def test_context_mismatch_rejected_and_file_unchanged(tmp_path):
    fname = "_triage_test_mismatch.py"
    actual_content = "x = 999\n"       # the file has 999
    # Build a diff that claims the file has 1 on that line
    stale_lines  = ["x = 1\n"]
    updated_lines = ["x = 2\n"]

    target = _make_src_file(fname, actual_content)
    try:
        diff = _unified_diff_str(f"src/{fname}", stale_lines, updated_lines)
        result = _apply_diff(diff)
        assert result is False, "Should reject on context/removal mismatch"
        assert target.read_text(encoding="utf-8") == actual_content, \
            "Original file must be untouched after rejection"
        # Temp file must not be left behind
        tmp = SRC_DIR / (fname + ".tmp_triage")
        assert not tmp.exists(), "No temp file should remain after rejection"
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Multi-line change — rejected (exceeds the 1-line limit)
# ---------------------------------------------------------------------------

def test_multiline_change_rejected():
    fname = "_triage_test_multiline.py"
    lines_orig = ["a = 1\n", "b = 2\n", "c = 3\n"]
    lines_new  = ["a = 10\n", "b = 20\n", "c = 3\n"]   # 2 lines changed

    target = _make_src_file(fname, "".join(lines_orig))
    try:
        diff = _unified_diff_str(f"src/{fname}", lines_orig, lines_new)
        result = _apply_diff(diff)
        assert result is False, \
            "A diff changing 2 lines must be rejected (limit is 1)"
        assert target.read_text(encoding="utf-8") == "".join(lines_orig), \
            "Original file must be untouched after multi-line rejection"
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5a. Path traversal via "../" — rejected
# ---------------------------------------------------------------------------

def test_path_traversal_rejected(tmp_path):
    # Craft a diff whose --- path tries to escape src/ via ../
    evil_diff = textwrap.dedent("""\
        --- a/../tests/test_calculator.py
        +++ b/../tests/test_calculator.py
        @@ -1,1 +1,1 @@
        -import pytest
        +import EVIL
    """)
    result = _apply_diff(evil_diff)
    assert result is False, "Path traversal outside src/ must be rejected"
    # Confirm test_calculator.py was not touched
    real = REPO_ROOT / "tests" / "test_calculator.py"
    assert "EVIL" not in real.read_text(encoding="utf-8"), \
        "test_calculator.py must not be modified"


# ---------------------------------------------------------------------------
# 5b. Path outside src/ (but inside repo) — rejected
# ---------------------------------------------------------------------------

def test_path_outside_src_rejected():
    # Target orchestrator/ci_triage.py — inside repo but NOT under src/
    evil_diff = textwrap.dedent("""\
        --- a/orchestrator/ci_triage.py
        +++ b/orchestrator/ci_triage.py
        @@ -1,1 +1,1 @@
        -\"\"\"
        +EVIL
    """)
    result = _apply_diff(evil_diff)
    assert result is False, "Paths outside src/ must be rejected even if inside repo"


# ---------------------------------------------------------------------------
# 6. No leftover temp file after rejection
# ---------------------------------------------------------------------------

def test_no_leftover_temp_file_on_rejection():
    fname = "_triage_test_tempclean.py"
    actual_content = "z = 999\n"
    stale_lines  = ["z = 1\n"]
    updated_lines = ["z = 2\n"]

    target = _make_src_file(fname, actual_content)
    tmp = SRC_DIR / (fname + ".tmp_triage")
    try:
        diff = _unified_diff_str(f"src/{fname}", stale_lines, updated_lines)
        _apply_diff(diff)   # will be rejected due to context mismatch
        assert not tmp.exists(), \
            ".tmp_triage file must not exist after a rejected patch"
    finally:
        target.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. Return values — True on success, False on rejection (two sub-cases)
# ---------------------------------------------------------------------------

def test_returns_true_on_success():
    fname = "_triage_test_ret_true.py"
    orig = "val = 'old'\n"
    new  = "val = 'new'\n"
    target = _make_src_file(fname, orig)
    try:
        diff = _unified_diff_str(f"src/{fname}", [orig], [new])
        assert _apply_diff(diff) is True
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


def test_returns_false_on_context_mismatch():
    fname = "_triage_test_ret_false.py"
    target = _make_src_file(fname, "real = 1\n")
    try:
        diff = _unified_diff_str(f"src/{fname}", ["wrong = 1\n"], ["wrong = 2\n"])
        assert _apply_diff(diff) is False
    finally:
        target.unlink(missing_ok=True)
        (SRC_DIR / (fname + ".tmp_triage")).unlink(missing_ok=True)


def test_returns_false_on_path_rejection():
    evil_diff = textwrap.dedent("""\
        --- a/AGENTS.md
        +++ b/AGENTS.md
        @@ -1,1 +1,1 @@
        -# Severity-Aware CI Triage
        +EVIL
    """)
    assert _apply_diff(evil_diff) is False
