---
name: unit-test-generator
description: Generates comprehensive pytest tests for a Python module, runs them, and reports failures without modifying source files.
---

1. Read the target Python module line by line; list every function, branch, and exception.
2. Generate pytest tests in `tests/test_<module>.py` covering normal, edge (zero, negative, float, empty, None), and error cases.
3. Run `pytest -q` and capture each failure traceback.
4. Report in format: `test name | file:line | exception type | message`.
5. Never modify source files; only report.
