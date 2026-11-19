# CI Permissions & Severity Rules

### Severity matrix

| Severity | Examples | Required behavior |
|---|---|---|
| LOW (1-line code bug) | syntax error, wrong arithmetic operator, type mismatch | 1. Diagnose root cause. 2. Generate a proposed unified diff as an Artifact. 3. Explicitly ask the user for permission. 4. Apply ONLY after an explicit "yes". |
| HIGH (DB/infra) | DB connection error, schema mismatch, .env or cloud config failure | 1. NEVER modify any code or config. 2. Pause execution. 3. Explain root cause. 4. Output a structured list of EXACTLY 3 corrective options for the user/admin to choose from. |
| UNKNOWN | anything unclassifiable | Treat as HIGH. |

### High-severity output format

```
SEVERITY: HIGH
ROOT CAUSE: <one or two sentences>
OPTIONS:
1. <option> | Risk: <low/med/high>
2. <option> | Risk: <low/med/high>
3. <option> | Risk: <low/med/high>
Awaiting admin choice. No changes made.
```

### Hard prohibitions (all severities)

- Never edit .env or any secrets file.
- Never auto-commit, push, or open PRs without permission.
- A fix may change at most 1 line; otherwise reclassify as HIGH.
- Silence or "ok" is not permission; only an explicit yes counts.
