# severity-gated-ci-triage

A human-gated multi-agent CI pipeline that classifies test failures by blast radius and enforces severity-proportional review before any fix is applied.

---

## How it works

```mermaid
flowchart TD
    A([Push / PR]) --> B[pytest -q --tb=short]
    B --> C{Any failures?}
    C -- No --> Z([Job green ✅])
    C -- Yes --> D[LogParser\nextract structured records]
    D --> E[Classifier\nheuristics + optional Gemini]
    E --> F{Severity?}

    F -- LOW\n1-line bug --> G[FixProposer\ngenerate unified diff]
    G --> H{Mode?}
    H -- local --interactive --> I[Ask user for explicit yes/no]
    H -- --ci / GitHub Actions --> J[Proposal only\nfix NOT applied]
    I -- yes --> K[Apply 1-line patch]
    I -- no / silence --> J

    F -- HIGH\nDB · infra · .env --> L[PolicyGate\nprint 3 options\nno file writes]
    F -- UNKNOWN --> L

    K --> M[Notifier]
    J --> M
    L --> M

    M --> N[Slack · Discord · Telegram · console]
    N --> O([Job stays red 🔴\nexit 1])
```

---

## Novelty — Severity-Aware Multi-Agent CI Triage with Human-in-the-Loop Gating

| Property | This project |
|---|---|
| **Autonomy scaled to blast radius** | LOW (1-line bug) → diff proposed; HIGH (DB / infra) → halted with options. The agent never acts beyond what the severity warrants. |
| **Permission gate is plain Python, not a prompt** | `permission_gate()` is deterministic logic — no LLM decides whether to write files. |
| **Unknown severity fails safe** | Anything unclassifiable is promoted to HIGH automatically. |
| **Offline mode needs no API key** | Without `GEMINI_API_KEY`, heuristics + operator-swap cover LOW fixes end-to-end. |
| **Fixes never applied in CI** | `--ci` flag hard-blocks all writes; the job posts the diff as a proposal only. |

---

## Project structure

```
ci-triage-agents/
├── .agents/
│   ├── rules/
│   │   └── ci-permissions.md      # severity matrix & hard prohibitions
│   └── skills/
│       ├── unit-test-generator/SKILL.md
│       └── novelty-analyzer/SKILL.md
├── .github/
│   └── workflows/
│       └── ci.yml
├── orchestrator/
│   └── ci_triage.py               # LogParser · Classifier · FixProposer · PolicyGate · Notifier
├── src/
│   ├── calculator.py              # intentional LOW bug (a - b)
│   └── db_connector.py            # intentional HIGH bug (schema mismatch)
├── tests/
│   ├── test_calculator.py
│   └── test_db_connector.py
├── AGENTS.md
├── requirements.txt
└── .env.example
```

---

## Rules summary

| Severity | Trigger examples | Agent behaviour |
|---|---|---|
| **LOW** | `AssertionError`, wrong operator, `TypeError` | Diagnose → generate unified diff → ask for explicit **yes** → apply only on yes |
| **HIGH** | `SchemaMismatchError`, `ConnectionError`, `.env` / `DATABASE_URL` failure | NEVER write files → pause → explain root cause → output exactly **3 options** |
| **UNKNOWN** | Anything unclassifiable | Treated as **HIGH** |

Hard prohibitions (all severities): never edit `.env` or secrets; never auto-commit/push/open PRs; a fix may change **at most 1 line**; silence is not permission.

---

## Demo

**1. Install dependencies**

```bash
python -m pip install -r requirements.txt
```

**2. Run the test suite — expect 2 failures**

```bash
python -m pytest
# 2 failed, 4 passed
```

**3. Capture a short traceback log**

PowerShell:
```powershell
python -m pytest -q --tb=short | Out-File -Encoding utf8 pytest.log
```

bash:
```bash
pytest -q --tb=short > pytest.log
```

**4. Run the triage orchestrator interactively**

```bash
python orchestrator/ci_triage.py --log pytest.log --interactive
```

**5. At the LOW-severity prompt**

- Answer `n` (or press Enter) → `git status` shows no changed files; the bug is preserved.
- Answer `y` → the `-` operator in `add()` is patched to `+` and the test now passes.

**6. Re-run pytest — only the DB failure remains**

```bash
python -m pytest -q
# 1 failed, 4 passed
```

**7. The HIGH-severity DB failure always halts with 3 options and never edits any file.**

**8. Restore the intentional bug before pushing**

```bash
git checkout src/calculator.py
```

**9. Push to GitHub and open the Actions tab**

The run will be red. Click the failing job → expand **Triage failures** to see the structured triage summary, or download the `ci-logs` artifact (`pytest.log` + `triage.md`).

**10. Optional: add repo secrets for online mode and notifications**

In **Settings → Secrets and variables → Actions**, add:

| Secret | Purpose |
|---|---|
| `GEMINI_API_KEY` | Enables Gemini-2.0-flash classification and fix proposals |
| `WEBHOOK_URL` | Slack / Discord webhook for triage alerts |
