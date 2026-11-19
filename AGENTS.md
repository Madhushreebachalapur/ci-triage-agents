# Severity-Aware CI Triage

**Scope:** CI only — testing, triage, PR checks, human-in-the-loop. No deployment or CD.

## Agent Roles

- **LogParser** — Ingests raw CI logs and extracts structured error records.
- **Classifier** — Assigns severity (LOW / HIGH / UNKNOWN) to each extracted error.
- **FixProposer** — Generates a proposed unified diff for LOW-severity errors, pending user approval.
- **PolicyGate** — Enforces ci-permissions.md rules before any action is taken.
- **Notifier** — Delivers triage summaries and awaits human decisions at HIGH-severity pause points.

## Skills Available

- `unit-test-generator` — `.agents/skills/unit-test-generator/SKILL.md`
- `novelty-analyzer` — `.agents/skills/novelty-analyzer/SKILL.md`

## Compliance

All agents MUST follow `.agents/rules/ci-permissions.md`. If rules conflict with a user request, ask first.
