---
name: novelty-analyzer
description: Checks whether proposed edge-case scenarios are genuinely new, duplicate, or only partially covered by existing code and tests.
---

1. List existing functions (names, signatures) in `src/` and existing test names in `tests/`.
2. For each proposed edge-case scenario, check if an existing function or test already covers it.
3. Mark each scenario: **NEW** (not covered), **DUPLICATE** (already covered), or **PARTIAL** (partly covered; say what is missing).
4. Only NEW and PARTIAL scenarios may be added. Output a table: `scenario | status | evidence`.
