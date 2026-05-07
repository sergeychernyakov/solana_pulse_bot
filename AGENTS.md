# AGENTS.md  <!-- Human-to-Agent instructions -->

## 🧠 General Agent Instructions
- You are an AI coding assistant working inside this container/repository.
- Follow every instruction in this file **unless** a direct user prompt overrides it.
- If something is unclear, ask clarifying questions rather than guessing.

---

## 🖥️ Where pulse_bot runs (CRITICAL)
- **Production (live `monitor`)**: only on **rich** server (`ssh rich`), via systemd user unit `pulse-bot.service`. Code path: `~/www/gg`. Logs: `~/www/gg/logs/bot.log`.
- **Mac**: development only — backtests, optimizer sweeps, ML training, dashboards. **Do not** run `python main.py monitor` on Mac. Mac PG is for dev queries / building datasets only.
- Backfill (`backfill.service`) and Solana validator (`solana-validator.service`) also run on rich via systemd.
- DB sync between rich (production) and Mac (dev) is **on-demand** (no cron). Pull from rich → Mac when refreshing dev data.

---

## 🛠️ Infrastructure on rich (CRITICAL — re-read before touching RPC/backfill)

Things that already exist and are **wired up**. Don't re-discover them; check here first.

### Local Solana validator
- **systemd unit**: `solana-validator.service` (user unit, auto-start)
- **JSON-RPC endpoint**: `http://127.0.0.1:8899`
- **Use case**: free unlimited RPC for `getSignaturesForAddress`, `getTransaction`, etc. — primary RPC for backfills.
- **Caveat**: may lag mainnet by N slots (`getHealth` shows `numSlotsBehind`). Historical queries still work; live queries may be stale.
- **Command to verify**: `curl -s -X POST -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' http://127.0.0.1:8899`
- **CRITICAL — UDP ports 8000-8020 MUST be open in UFW** for the validator to receive turbine shreds and repair responses. Without this, the validator silently fails to catch up (logs `repair_peers=0`, `num_shreds_received=0`, `bank_status: Unprocessed`). Verify with `sudo ufw status | grep 8000:8020`. Fix: `sudo ufw allow 8000:8020/udp; sudo ufw allow 8000:8020/tcp`. Rules persist in `/etc/ufw/user.rules`.
- **Diagnostic when stuck**: `getSlot` shows root, `getFirstAvailableBlock` shows ledger window. If root not advancing — check `sudo grep "UFW BLOCK" /var/log/syslog | grep "DST=192.168.3.118"` for blocked shreds.

### Helius API keys
- **Three free-tier keys** stored in `/home/sergey/www/gg/.env`:
  - `HELIUS_API_KEY=<single>` (legacy)
  - `HELIUS_API_KEYS=key1,key2,key3` (rotation)
  - `PUBLIC_RPC_URLS=http://127.0.0.1:8899,https://mainnet.helius-rpc.com/?api-key=key1,…` (validator first, Helius fallback)
- Keys are **also** encoded into `~/.config/systemd/user/backfill.service` as `Environment="HELIUS_API_KEYS=…"` and `Environment="PUBLIC_RPC_URLS=http://127.0.0.1:8899,…"`. Source-of-truth: keep both in sync if you rotate.
- **Free-tier limits**: ~10 RPS / 100k req/day per key. Spamming with 50 concurrent calls hits 429 immediately. Use `--mint-parallelism 5 --concurrency 8` defaults for `helius_backfill_graduated.py`.
- **Bug fixed 2026-04-29**: `scripts/helius_backfill_graduated.py` crashed on empty `PUBLIC_RPC_URLS` — auto-builds from `HELIUS_API_KEYS` now.

### Backfill systemd service
- **Unit**: `backfill.service` (user unit). Source: `~/.config/systemd/user/backfill.service`.
- Runs `helius_backfill_graduated.py` with the canonical env (validator + 3 helius keys).
- **Idempotent**: per-mint resume state in `data/backfill_state.json`.

### Database (PostgreSQL)
- **DSN**: `PULSE_PG_DSN=postgresql://sergeychernyakov:pulsebot@localhost/pulse_bot` in `.env`.
- **Note**: standalone scripts MUST `set -a && source .env && set +a` before running, otherwise `_resolve_dsn` falls back to a passwordless DSN that fails authentication on rich.

### Standalone script invocation pattern on rich
```bash
ssh rich
cd /home/sergey/www/gg
set -a && source .env && set +a   # load PG creds + Helius keys
PYTHONPATH=. .venv/bin/python -m <module>
```

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
