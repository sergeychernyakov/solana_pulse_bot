# .claude/agents/SHARED_CONFIG.md

# Shared Configuration for All Agents

This file contains common configurations that all agents should reference.

## Project Structure
```
src/
  models/       # Data models
  services/     # Service objects
  utils/        # Utility functions
  config/       # Configuration
tests/          # Mirror src/ structure
artifacts/      # Agent outputs
```

## Naming Conventions
- Files: **snake_case** (e.g., `user_service.py`)
- Classes: **CamelCase** (e.g., `UserService`)
- First line of each file: `# path/to/file.py`

## Code Standards
- Use type hints everywhere
- Google-style docstrings for all public APIs
- Use `logging` module, not `print()`
- Methods: ≤50 lines
- Classes: ≤300 lines

## Testing
- Use **pytest** with AAA pattern (Arrange → Act → Assert)
- Target **90%+** code coverage
- Tests in `tests/` mirroring `src/` structure
- Always use `.venv/bin/pytest`

## Pre-commit Hooks
- Run: `pre-commit run --all-files`
- Required hooks:
  - `black` - Code formatting
  - `isort` - Import sorting
  - `ruff` - Linting
  - `mypy` - Type checking
  - `pylint` - Code analysis (target ≥ 9.0)

## Security
- **NEVER** include hardcoded secrets or keys
- **NEVER** use `--no-verify` in git commands
- **ALWAYS** validate input data
- Use environment variables for secrets

## Error Handling
- No bare `except:` clauses
- Access attributes directly when possible
- Fail fast with explicit errors

## Agent Communication
- Planner: `artifacts/planner_plan.md`, `artifacts/planner_report.md`
- Architect: `artifacts/architect_design.md`
- Senior Coder: `artifacts/senior_coder_result.md`
- Code Reviewer: `artifacts/reviewer_notes.md`
- QA Engineer: `artifacts/qa_report.md`

## Quality Gates
- ✓ Pre-commit hooks pass
- ✓ Tests pass with ≥90% coverage
- ✓ No CRITICAL findings from reviewer
- ✓ Static analysis clean (ruff, mypy)

## End of Shared Configuration
