# AGENTS.md  <!-- Human-to-Agent instructions -->

## 🧠 General Agent Instructions
- You are an AI coding assistant working inside this container/repository.
- Follow every instruction in this file **unless** a direct user prompt overrides it.
- If something is unclear, ask clarifying questions rather than guessing.

---

## 📖 Project Description
See **[`README.md`](./README.md)** for an overview of the project’s purpose, requirements and architecture.

---

## 📐 Coding Conventions
- Follow the style guide in **[`PYTHON_STYLE_GUIDE.md`](./PYTHON_STYLE_GUIDE.md)**.
- File names: **snake_case** (`email_service.py`), class names: **CamelCase** (`EmailService`).
- First line of each code file must be a comment with the file path, e.g.
  `# src/services/email_service.py`
- Use type hints everywhere and Google-style docstrings for all public APIs.
- Do **not** use `print()` for output—use the standard `logging` module.

---

## 🧪 Testing & Quality
- Use **pytest**; follow Arrange → Act → Assert.
- Place tests in a mirroring structure under `tests/`.
- Target **90 %+** code coverage.
- Ensure all linters/formatters (e.g. `black`, `isort`, `ruff`, `pylint`) pass before finishing.

---

## 🔐 Security & Secrets
- Never hardcode secrets—use environment variables or a secret manager.
- Sanitize user inputs and escape web outputs.
- Run dependency audit tools (`pip-audit`, `safety`, etc.) if relevant.

---

## 🤖 Agent Limitations
- Do **not** execute system commands unless explicitly told to.
- Do **not** commit or push to git; source-control steps are handled outside this agent.
- Never overwrite user data without confirmation.
- If a required decision is ambiguous—ask.

---

## 📝 Changelog (REQUIRED)

**Rule:** any change that affects the ML model or bot's trading behavior MUST be recorded in **[`docs/CHANGELOG.md`](./docs/CHANGELOG.md)** before the task is considered done.

**What qualifies (obligatory entry):**
- Model retrain (before/after metrics + hash)
- Config change affecting live behavior (`exit_*`, `entry_*`, `score_threshold_*`, `exit_ml_*`, `entry_ml_*`)
- Schema bump (feature added/removed, `FEATURE_SCHEMA_VERSION` change)
- Threshold override in `meta.json` (manual proba floor/ceiling)
- Bot restart with new config or code
- Feature stability protocol run result
- Optimizer sweep result that changed config defaults
- Rollback of any of the above

**What does NOT require an entry:**
- Code refactors without behavior change
- Test-only changes
- Documentation-only updates (except this doc)
- Temporary diagnostic scripts

**Entry format** (documented in top of CHANGELOG.md):
```
## YYYY-MM-DD HH:MM — Title
**What changed:** one sentence
**Why:** reason / triggering finding
**Result:** metrics (before → after) + model hash if applicable
**Rollback:** how to revert
```

**Enforcement:** if you finish a change and forgot CHANGELOG, add the entry before closing the task. When in doubt — write the entry. Under-documenting is worse than noisy history.

---

## ✅ Final Deliverables Checklist
- [ ] Code adheres to `PYTHON_STYLE_GUIDE.md`.
- [ ] All tests pass (`pytest -q`) with ≥ 90 % coverage.
- [ ] Linting/formatting passes (`black`, `isort`, `ruff`, `pylint ≥ 9.5`).
- [ ] No hard-coded secrets; environment variables used where necessary.
- [ ] Any setup or run instructions updated in `README.md` if required.
- [ ] **CHANGELOG.md updated** for any model/bot-behavior change (see §📝 Changelog).
