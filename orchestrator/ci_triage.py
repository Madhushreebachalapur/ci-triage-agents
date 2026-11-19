"""
CI Triage Orchestrator
Agents: LogParser, Classifier, FixProposer, PolicyGate, Notifier
Compliance: .agents/rules/ci-permissions.md
"""

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# ---------------------------------------------------------------------------
# 1. LogParser
# ---------------------------------------------------------------------------

def parse_log(path: str) -> list[dict]:
    """Parse `pytest -q --tb=short` output into structured failure records."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")

    failures = []
    parsed_names: set[str] = set()

    # pytest --tb=short block header: "_ test_name _" (underscores, spaces, test name)
    # Capture block content between headers
    block_pattern = re.compile(
        r"_{3,} ([\w\[\] -]+) _{3,}\r?\n(.*?)(?=_{3,}|\Z)",
        re.DOTALL,
    )

    # Summary line: FAILED tests/foo.py::test_bar - ExcType: msg  OR  - assert ...
    summary_pattern = re.compile(
        r"FAILED\s+([\w/\\.\-]+)::([\w\[\]-]+)\s+-\s+(.*)"
    )

    # Build a map from test_name -> (file, exc, msg) from summary lines
    summary_map: dict[str, dict] = {}
    for m in summary_pattern.finditer(text):
        tfile, tname, rest = m.group(1), m.group(2), m.group(3).strip()
        m2 = re.match(r"([\w.]+(?:Error|Exception|Mismatch)):\s*(.*)", rest)
        if m2:
            exc = m2.group(1).split(".")[-1]
            msg = m2.group(2).strip()
        else:
            exc = "AssertionError"
            msg = rest
        summary_map[tname] = {"test_file": tfile, "exception_type": exc, "message": msg}

    for m in block_pattern.finditer(text):
        test_name = m.group(1).strip()
        block = m.group(2)

        # Skip the summary section block (title contains "short test summary")
        if "short test summary" in test_name.lower() or "FAILURES" in test_name:
            continue

        line_no = None
        exc_type = "UnknownError"
        message = ""
        func_under_test = ""
        test_file = ""

        # Pull exc/msg/file from summary map if available
        if test_name in summary_map:
            sm = summary_map[test_name]
            test_file = sm["test_file"]
            exc_type = sm["exception_type"]
            message = sm["message"]

        # Refine exc_type/message from "E " lines in the block (more complete)
        e_lines = re.findall(r"^\s*E\s+(.+)$", block, re.MULTILINE)
        if e_lines:
            for el in e_lines:
                el = el.strip()
                m2 = re.match(r"([\w.]+(?:Error|Exception|Mismatch)):\s*(.*)", el)
                if m2:
                    exc_type = m2.group(1).split(".")[-1]  # strip module prefix
                    message = m2.group(2).strip()
                    break
                # bare assert failure: "assert -1 == 5"
                if el.startswith("assert "):
                    exc_type = "AssertionError"
                    message = el
                    break

        # Line number from first "file.py:NN:" in block
        ln_m = re.search(r"[\w/\\]+\.py:(\d+):", block)
        if ln_m:
            line_no = int(ln_m.group(1))

        # Function under test from lines starting with ">  " (long tb) or "    assert/call" (short tb)
        call_m = re.search(r"(?:>\s+|^\s{4,})(?:assert\s+)?(\w+)\s*\(", block, re.MULTILINE)
        if call_m:
            func_under_test = call_m.group(1)

        failures.append({
            "test_name": test_name,
            "test_file": test_file,
            "line": line_no,
            "exception_type": exc_type,
            "message": message,
            "func_under_test": func_under_test,
            "_raw_block": block,
        })
        parsed_names.add(test_name)

    # Catch any FAILED tests not found by block parser
    for tname, sm in summary_map.items():
        if tname not in parsed_names:
            failures.append({
                "test_name": tname,
                "test_file": sm["test_file"],
                "line": None,
                "exception_type": sm["exception_type"],
                "message": sm["message"],
                "func_under_test": "",
                "_raw_block": "",
            })

    return failures


# ---------------------------------------------------------------------------
# 2. Classifier
# ---------------------------------------------------------------------------

LOW_EXC = {"AssertionError", "SyntaxError", "TypeError", "ValueError"}
HIGH_SIGNALS = {
    "ConnectionError", "OperationalError", "SchemaMismatchError",
    "InterfaceError", "DatabaseError",
}
HIGH_MSG_PATTERNS = re.compile(
    r"column|schema|connection|\.env|DATABASE_URL|cloud config|credentials|migration",
    re.IGNORECASE,
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
_OFFLINE = not GEMINI_API_KEY

if not _OFFLINE:
    print("ONLINE MODE (Gemini API key detected)")
else:
    print("OFFLINE MODE")


def _heuristic_severity(failure: dict) -> str:
    exc = failure["exception_type"]
    msg = failure["message"]
    if exc in HIGH_SIGNALS or HIGH_MSG_PATTERNS.search(msg) or HIGH_MSG_PATTERNS.search(exc):
        return "HIGH"
    if exc in LOW_EXC:
        return "LOW"
    return "UNKNOWN"


def _ask_gemini_classify(failure: dict) -> dict | None:
    if _OFFLINE or not HAS_REQUESTS:
        return None
    prompt = (
        "You are a CI triage classifier. Given this test failure, reply with ONLY valid JSON "
        '{"severity": "LOW"|"HIGH"|"UNKNOWN", "confidence": 0.0-1.0, "root_cause": "one sentence"}.\n\n'
        f"test_name: {failure['test_name']}\n"
        f"exception_type: {failure['exception_type']}\n"
        f"message: {failure['message']}\n"
        f"raw_output:\n{failure['_raw_block'][:800]}"
    )
    try:
        import google.genai as genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception:
        return None


def classify(failure: dict) -> dict:
    """Classifier agent: returns {severity, root_cause, confidence}."""
    heuristic = _heuristic_severity(failure)
    gemini_result = _ask_gemini_classify(failure)

    if gemini_result:
        g_sev = gemini_result.get("severity", "UNKNOWN").upper()
        confidence = gemini_result.get("confidence", 0.5)
        root_cause = gemini_result.get("root_cause", "")
        # If Gemini disagrees with heuristic or unclassifiable, use HIGH
        if g_sev == "UNKNOWN" or (heuristic == "LOW" and g_sev == "HIGH"):
            severity = "HIGH"
        elif heuristic == "HIGH":
            severity = "HIGH"
        else:
            severity = g_sev
    else:
        confidence = 0.8
        severity = "HIGH" if heuristic == "UNKNOWN" else heuristic
        exc = failure["exception_type"]
        msg = failure["message"]
        if severity == "LOW":
            root_cause = (
                f"The function under test returned an unexpected value. "
                f"{exc} raised: {msg}"
            )
        else:
            root_cause = (
                f"{exc} detected: {msg}. "
                "This is a database/infrastructure or unclassifiable error requiring admin review."
            )

    return {"severity": severity, "root_cause": root_cause, "confidence": confidence}


# ---------------------------------------------------------------------------
# 3. FixProposer
# ---------------------------------------------------------------------------

OPERATOR_SWAPS = {"+": "-", "-": "+", "*": "/", "/": "*"}


def _find_function_source(func_name: str) -> tuple[Path, int, list[str]] | None:
    """Return (file_path, start_lineno_0indexed, all_file_lines) for func_name in src/."""
    for py_file in SRC_DIR.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines(keepends=True)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                return py_file, node.lineno - 1, lines
    return None


def _make_diff(original_lines: list[str], new_lines: list[str], filepath: Path) -> str:
    rel = str(filepath.relative_to(REPO_ROOT))
    return "".join(
        difflib.unified_diff(original_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )


def _try_operator_swap(func_info, failure: dict) -> str | None:
    """Offline: try swapping arithmetic operators until the test passes."""
    src_file, func_start, orig_lines = func_info
    test_id = f"{failure['test_file']}::{failure['test_name']}"

    # Find lines inside the function that contain an operator
    for i in range(func_start, min(func_start + 20, len(orig_lines))):
        line = orig_lines[i]
        for old_op, new_op in OPERATOR_SWAPS.items():
            # Only swap standalone operators (surrounded by spaces or operands)
            pattern = rf"(?<=[a-zA-Z0-9_\s]){re.escape(old_op)}(?=[a-zA-Z0-9_\s])"
            if re.search(pattern, line):
                new_line = re.sub(pattern, new_op, line, count=1)
                if new_line == line:
                    continue
                candidate = orig_lines.copy()
                candidate[i] = new_line

                # Write to temp file, run only the failing test
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False,
                    dir=src_file.parent, prefix="_tmp_triage_"
                ) as tf:
                    tmp_path = Path(tf.name)
                    tf.writelines(candidate)

                # Swap the real file temporarily
                orig_backup = src_file.read_bytes()
                try:
                    src_file.write_bytes(tmp_path.read_bytes())
                    result = subprocess.run(
                        [sys.executable, "-m", "pytest", test_id, "-q", "--tb=no"],
                        capture_output=True, text=True, cwd=REPO_ROOT
                    )
                    if result.returncode == 0:
                        diff = _make_diff(orig_lines, candidate, src_file)
                        return diff
                finally:
                    src_file.write_bytes(orig_backup)
                    tmp_path.unlink(missing_ok=True)
    return None


def _ask_gemini_fix(func_info, failure: dict) -> str | None:
    if _OFFLINE or not HAS_REQUESTS:
        return None
    src_file, func_start, orig_lines = func_info
    # Send just the function lines
    func_lines = orig_lines[func_start: func_start + 15]
    prompt = (
        "You are a CI fix proposer. The test below fails. "
        "Return ONLY the corrected single line (no markdown, no explanation).\n\n"
        f"File: {src_file}\nFunction start line: {func_start + 1}\n"
        f"Function source:\n{''.join(func_lines)}\n\n"
        f"Failing test: {failure['test_name']}\n"
        f"Exception: {failure['exception_type']}: {failure['message']}\n"
        "Reply with ONLY the corrected line, preserving indentation."
    )
    try:
        import google.genai as genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception:
        return None


def propose_fix(failure: dict) -> str | None:
    """FixProposer agent: produce a unified diff for LOW-severity failures only."""
    func_name = failure.get("func_under_test", "")
    if not func_name:
        return None

    func_info = _find_function_source(func_name)
    if func_info is None:
        return None

    src_file, func_start, orig_lines = func_info

    diff = None

    if not _OFFLINE:
        fixed_line = _ask_gemini_fix(func_info, failure)
        if fixed_line:
            # Find which line in the function to replace (the return/suspect line)
            for i in range(func_start, min(func_start + 15, len(orig_lines))):
                stripped = orig_lines[i].strip()
                if stripped.startswith("return") or any(op in stripped for op in "+-*/"):
                    candidate = orig_lines.copy()
                    # Preserve indentation
                    indent = len(orig_lines[i]) - len(orig_lines[i].lstrip())
                    candidate[i] = " " * indent + fixed_line.lstrip() + "\n"
                    changed = sum(1 for a, b in zip(orig_lines, candidate) if a != b)
                    if changed <= 1:
                        diff = _make_diff(orig_lines, candidate, src_file)
                    else:
                        diff = None  # reclassify trigger
                    break

    if diff is None:
        diff = _try_operator_swap(func_info, failure)

    if diff is None:
        return None

    # Hard rule: reject diffs changing more than 1 line
    changed_lines = [l for l in diff.splitlines() if l.startswith("+") or l.startswith("-")]
    actual_changes = [l for l in changed_lines if not l.startswith("+++") and not l.startswith("---")]
    additions = [l for l in actual_changes if l.startswith("+")]
    deletions = [l for l in actual_changes if l.startswith("-")]
    if len(additions) > 1 or len(deletions) > 1:
        return None  # Must reclassify as HIGH upstream

    return diff


# ---------------------------------------------------------------------------
# 4. PolicyGate
# ---------------------------------------------------------------------------

HIGH_OPTIONS_TEMPLATES = {
    "schema": [
        ("Fix the column name in the calling code and open a PR for review", "low"),
        ("Run a schema migration to add or rename the column in the database", "med"),
        ("Add a database compatibility view that maps `user_name` to `username`", "med"),
    ],
    "connection": [
        ("Verify DATABASE_URL is correctly set in the environment / secrets manager", "low"),
        ("Check network connectivity and credentials to the database host", "med"),
        ("Restore configuration from the last known-good environment snapshot", "high"),
    ],
    "default": [
        ("Investigate the root cause manually and open a PR with the fix", "low"),
        ("Roll back the last deployment or config change and re-run CI", "med"),
        ("Escalate to the on-call engineer for infrastructure review", "high"),
    ],
}


def _pick_options(root_cause: str) -> list[tuple[str, str]]:
    rc_lower = root_cause.lower()
    if any(w in rc_lower for w in ("column", "schema", "mismatch")):
        return HIGH_OPTIONS_TEMPLATES["schema"]
    if any(w in rc_lower for w in ("connection", "database_url", ".env", "credentials")):
        return HIGH_OPTIONS_TEMPLATES["connection"]
    return HIGH_OPTIONS_TEMPLATES["default"]


def _apply_diff(diff: str) -> bool:
    """
    Apply a unified diff to the working tree using a safe hunk applier.

    Safety guarantees (all enforced before any file is written):
    - Target path must resolve inside REPO_ROOT/src/; rejects path traversal.
    - Every context and '-' line is verified against the actual file content
      at the expected position; mismatch → reject, nothing written.
    - Total changed lines (additions + deletions across all hunks) must be <= 1.
    - Result line count must equal original + added - removed.
    - Writes to a sibling temp file, then os.replace(); original untouched on error.

    Returns True on success, False on any rejection (prints reason).
    """
    import os as _os

    # ------------------------------------------------------------------ #
    # Phase 1: parse the diff into (src_path, list_of_hunks)             #
    # ------------------------------------------------------------------ #
    src_file: Path | None = None
    hunks: list[dict] = []          # {orig_start, orig_len, add_len, lines}

    diff_lines = diff.splitlines(keepends=True)

    i = 0
    while i < len(diff_lines):
        line = diff_lines[i]

        if line.startswith("--- "):
            # Normalize: strip "a/" prefix and convert backslashes
            rel = line[4:].split("\t")[0].strip().replace("\\", "/").lstrip("a/")
            candidate = (REPO_ROOT / rel).resolve()

            # Path safety: must be inside repo and inside src/
            try:
                candidate.relative_to(REPO_ROOT.resolve())
            except ValueError:
                print(f"[_apply_diff] REJECTED: path escapes repo root: {candidate}")
                return False
            try:
                candidate.relative_to((REPO_ROOT / "src").resolve())
            except ValueError:
                print(f"[_apply_diff] REJECTED: path is outside src/: {candidate}")
                return False

            src_file = candidate
            i += 1
            continue

        if line.startswith("+++ "):
            i += 1
            continue

        if line.startswith("@@ "):
            # Parse @@ -orig_start[,orig_len] +new_start[,new_len] @@
            hunk_m = re.match(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line
            )
            if not hunk_m:
                print(f"[_apply_diff] REJECTED: malformed hunk header: {line!r}")
                return False
            orig_start = int(hunk_m.group(1))          # 1-based
            orig_len   = int(hunk_m.group(2) or "1")
            new_len    = int(hunk_m.group(4) or "1")

            # Collect hunk body lines
            body: list[str] = []
            i += 1
            while i < len(diff_lines) and not diff_lines[i].startswith("@@") \
                    and not diff_lines[i].startswith("--- ") \
                    and not diff_lines[i].startswith("+++ "):
                body.append(diff_lines[i])
                i += 1

            hunks.append({
                "orig_start": orig_start,  # 1-based
                "orig_len":   orig_len,
                "new_len":    new_len,
                "body":       body,
            })
            continue

        i += 1

    if src_file is None:
        print("[_apply_diff] REJECTED: no --- path line found in diff.")
        return False

    if not src_file.exists():
        print(f"[_apply_diff] REJECTED: target file does not exist: {src_file}")
        return False

    # ------------------------------------------------------------------ #
    # Phase 2: count total changed lines across all hunks                 #
    # ------------------------------------------------------------------ #
    total_added   = sum(
        sum(1 for l in h["body"] if l.startswith("+")) for h in hunks
    )
    total_removed = sum(
        sum(1 for l in h["body"] if l.startswith("-")) for h in hunks
    )
    total_changed = total_added + total_removed

    if total_changed > 2:   # 1 removal + 1 addition == a single-line swap
        print(
            f"[_apply_diff] REJECTED: diff changes {total_changed // 2} lines "
            f"(added={total_added}, removed={total_removed}); limit is 1 line."
        )
        return False

    # ------------------------------------------------------------------ #
    # Phase 3: load file, verify context & '-' lines, apply hunks        #
    # ------------------------------------------------------------------ #
    orig_text  = src_file.read_text(encoding="utf-8")
    orig_lines = orig_text.splitlines(keepends=True)

    # Work on a mutable copy; apply hunks in REVERSE order so earlier
    # hunk positions stay valid after insertions/deletions.
    result = orig_lines.copy()
    offset = 0  # cumulative line-count delta from previously applied hunks

    for hunk in reversed(hunks):
        orig_start_0 = hunk["orig_start"] - 1  # convert to 0-based
        body          = hunk["body"]

        # Verify context and '-' lines against the ORIGINAL file
        file_pos = orig_start_0
        for bline in body:
            if bline.startswith("+"):
                continue
            # context or '-' line: strip the leading ' ' or '-'
            expected = bline[1:]
            if file_pos >= len(orig_lines):
                print(
                    f"[_apply_diff] REJECTED: hunk runs past end of file "
                    f"(expected line {file_pos + 1}, file has {len(orig_lines)} lines)."
                )
                return False
            actual = orig_lines[file_pos]
            # Compare ignoring trailing \r differences only
            if actual.rstrip("\r\n") != expected.rstrip("\r\n"):
                print(
                    f"[_apply_diff] REJECTED: context/removal mismatch at line {file_pos + 1}.\n"
                    f"  Expected: {expected!r}\n"
                    f"  Actual  : {actual!r}"
                )
                return False
            file_pos += 1

        # Apply the hunk to `result` (which is offset-adjusted)
        result_start = orig_start_0 + offset
        # Collect lines the hunk consumes from result (context + removed)
        consume = sum(1 for l in body if not l.startswith("+"))
        # Build the replacement slice
        replacement = []
        for bline in body:
            if bline.startswith("-"):
                continue          # drop removed lines
            # context (space) or added (+): keep the content (strip marker)
            replacement.append(bline[1:])

        result[result_start : result_start + consume] = replacement
        offset += len(replacement) - consume

    # ------------------------------------------------------------------ #
    # Phase 4: validate result line count                                 #
    # ------------------------------------------------------------------ #
    expected_len = len(orig_lines) + total_added - total_removed
    if len(result) != expected_len:
        print(
            f"[_apply_diff] REJECTED: result has {len(result)} lines, "
            f"expected {expected_len}."
        )
        return False

    # ------------------------------------------------------------------ #
    # Phase 5: atomic write via temp file + os.replace()                 #
    # ------------------------------------------------------------------ #
    tmp_path = src_file.with_suffix(src_file.suffix + ".tmp_triage")
    try:
        tmp_path.write_text("".join(result), encoding="utf-8")
        _os.replace(str(tmp_path), str(src_file))
    except Exception as exc:
        print(f"[_apply_diff] ERROR during write: {exc}. Original file untouched.")
        tmp_path.unlink(missing_ok=True)
        return False

    print(f"[_apply_diff] Patch applied successfully to {src_file.name}.")
    return True


def permission_gate(
    failure: dict,
    severity: str,
    root_cause: str,
    diff: str | None,
    interactive: bool,
    ci: bool,
) -> bool:
    """PolicyGate agent. Returns True if a fix was applied."""
    test_name = failure["test_name"]

    if severity == "HIGH" or severity == "UNKNOWN":
        options = _pick_options(root_cause)
        print(f"\n{'='*60}")
        print(f"SEVERITY: HIGH")
        print(f"ROOT CAUSE: {root_cause}")
        print("OPTIONS:")
        for idx, (opt, risk) in enumerate(options, 1):
            print(f"  {idx}. {opt} | Risk: {risk}")
        print("Awaiting admin choice. No changes made.")
        print(f"{'='*60}\n")
        return False

    # LOW severity
    if diff is None:
        print(f"\n[PolicyGate] LOW severity but no diff could be generated for {test_name}.")
        return False

    print(f"\n{'='*60}")
    print(f"SEVERITY: LOW  |  Test: {test_name}")
    print("Proposed fix (unified diff):")
    print(diff)
    print(f"{'='*60}")

    if ci:
        print("[PolicyGate] --ci mode: fix proposed but NOT applied. Human review required.")
        return False

    if interactive:
        answer = input("Apply this fix? [y/N]: ").strip().lower()
        if answer in ("y", "yes"):
            applied = _apply_diff(diff)
            if applied:
                print("[PolicyGate] Fix applied.")
                return True
            else:
                print("[PolicyGate] Fix was REJECTED by _apply_diff; original file unchanged.")
                return False
        else:
            print("[PolicyGate] Fix NOT applied (no explicit 'yes').")
            return False

    print("[PolicyGate] Neither --interactive nor --ci: fix NOT applied.")
    return False


# ---------------------------------------------------------------------------
# 5. Notifier
# ---------------------------------------------------------------------------

def notify(message: str):
    """Notifier agent: send alert to webhook or fall back to console."""
    webhook_url = os.getenv("WEBHOOK_URL", "")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")

    if not HAS_REQUESTS or not webhook_url and not telegram_token:
        print(f"\n[Notifier] {message}")
        return

    sent = False

    if webhook_url:
        try:
            if "hooks.slack.com" in webhook_url:
                payload = {"text": message}
            elif "discord.com/api/webhooks" in webhook_url:
                payload = {"content": message[:2000]}
            else:
                payload = {"text": message}
            resp = requests.post(webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            sent = True
        except Exception as e:
            print(f"[Notifier] Webhook failed ({e}), falling back to console.")

    if not sent and telegram_token and telegram_chat:
        try:
            url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            resp = requests.post(
                url, json={"chat_id": telegram_chat, "text": message[:4096]}, timeout=10
            )
            resp.raise_for_status()
            sent = True
        except Exception as e:
            print(f"[Notifier] Telegram failed ({e}), falling back to console.")

    if not sent:
        print(f"\n[Notifier] {message}")


def _build_notify_message(failure: dict, severity: str, root_cause: str, diff: str | None, options: list) -> str:
    lines = [
        f"[CI TRIAGE ALERT]",
        f"Test     : {failure['test_name']}",
        f"File     : {failure['test_file']}",
        f"Severity : {severity}",
        f"Root Cause: {root_cause}",
    ]
    if severity == "LOW" and diff:
        lines.append("Proposed Diff:")
        lines.append(diff)
    elif options:
        lines.append("Options:")
        for idx, (opt, risk) in enumerate(options, 1):
            lines.append(f"  {idx}. {opt} | Risk: {risk}")
        lines.append("Awaiting admin choice. No changes made.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CI Triage Orchestrator")
    parser.add_argument("--log", required=True, help="Path to pytest log file")
    parser.add_argument("--interactive", action="store_true", help="Prompt user to apply fixes")
    parser.add_argument("--ci", action="store_true", help="CI mode: never apply fixes")
    args = parser.parse_args()

    failures = parse_log(args.log)

    if not failures:
        print("No failures found in log.")
        sys.exit(0)

    any_failure = False

    for failure in failures:
        any_failure = True
        result = classify(failure)
        severity = result["severity"]
        root_cause = result["root_cause"]
        confidence = result["confidence"]

        print(f"\n--- Triaging: {failure['test_name']} ---")
        print(f"Exception : {failure['exception_type']}: {failure['message']}")
        print(f"Classified: {severity} (confidence={confidence:.2f})")
        print(f"Root Cause: {root_cause}")

        diff = None
        options = []

        if severity == "LOW":
            diff = propose_fix(failure)
            if diff is None:
                print("[FixProposer] Could not generate a 1-line diff; treating as HIGH.")
                severity = "HIGH"
                root_cause += " (fix exceeded 1 line or could not be determined)"
                options = _pick_options(root_cause)

        if severity in ("HIGH", "UNKNOWN"):
            options = _pick_options(root_cause)

        permission_gate(failure, severity, root_cause, diff, args.interactive, args.ci)

        notify_msg = _build_notify_message(failure, severity, root_cause, diff, options)
        notify(notify_msg)

    if any_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
