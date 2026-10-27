# Commands Reference

The commands CI runs, by phase. `CONTRIBUTING.md` explains the test markers and the fresh-install gate.

## Setup (once)

```bash
pip install -e ".[dev]"
pre-commit install                 # gitleaks, ruff, instance-leak, CLAUDE.md size, deleted refs
npm ci                             # commitlint for the commit-msg hook
```

## Tests

```bash
# Fast unit suite, off any real database (use while developing)
pytest tests/ robothor/ -m "not slow and not llm and not e2e and not integration"

# What CI runs for the Python unit suite
pytest tests/ robothor/ -m "not integration and not llm and not slow and not e2e" --cov=robothor --cov-fail-under=60

# One module, one file, one test
pytest robothor/memory/tests/ -v
pytest robothor/engine/tests/test_<feature>.py -v
pytest robothor/engine/tests/test_<feature>.py -k "<name>" -v

# Bridge, and integration tests (need Postgres + Redis), as CI runs them
pytest crm/bridge/tests/ -m "not integration and not slow and not e2e"
pytest tests/ robothor/ -m "integration and not llm" -v
pytest crm/ -m "integration and not llm" -v
```

## Static Analysis

```bash
ruff check robothor/ bench/ scripts/
ruff format --check robothor/ bench/ scripts/
mypy robothor/ --ignore-missing-imports
shellcheck --severity=warning scripts/*.sh
pre-commit run --all-files         # every hook against the whole tree
```

## Docs

```bash
python scripts/check_doc_links.py      # every path the docs reference exists
python scripts/check_doc_commands.py   # every command quoted in the docs parses
pytest tests/test_check_doc_links.py
python scripts/gen_cli_doc.py          # after a CLI verb or flag change
python scripts/gen_configuration_doc.py   # after a settings change
```

## Release Notes

```bash
python3 scripts/changelog_fragments.py lint
python3 scripts/changelog_fragments.py check --pr <n> --title "<the PR title>"
```

## Dashboard (`app/`)

```bash
cd app && pnpm install --frozen-lockfile
pnpm lint && pnpm exec tsc --noEmit && pnpm test
pnpm exec playwright test          # user flows
```

## Agent Manifests

```bash
python scripts/validate_agents.py --agent <id>
```

## Pull Requests

```bash
gh pr create --title "type(scope): description (KEY-N)" --body-file <file>
gh pr view <n> --comments          # threads to answer
gh pr checks <n>
```

Requesting the two reviews uses deployment-specific commands; they live in the operator's private instructions.
