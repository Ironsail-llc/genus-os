# TDD Guide for Coding Agents

Root `CLAUDE.md` rule 8 makes test-first non-negotiable. Exempt: typos, docs, config-only edits.

## The Core Problem

An agent's default is implementation-first: write the code, then tests that confirm the code it just wrote. That defeats TDD. **Enforce the phase separation explicitly**, one phase at a time.

## Red-Green-Refactor

### RED (failing tests)

Write tests for the behaviour as if the function or class does not exist yet. Do NOT write implementation code.

```bash
pytest robothor/<module>/tests/test_<feature>.py -v   # must FAIL, for the reason you expect
```

Confirm the failure is the missing behaviour, not an import typo or a broken fixture. Commit the tests only: `test(<scope>): <behaviour> (KEY-N)`.

### GREEN (minimal implementation)

The MINIMUM code that makes those tests pass. No extra features, no premature optimisation.

```bash
pytest robothor/<module>/tests/ -v                     # must PASS
```

Commit: `feat(<scope>): …` or `fix(<scope>): …`, ending `(KEY-N)`.

### REFACTOR (clean up)

Improve readability and remove duplication. Run the tests after every step; they stay green. Commit: `refactor(<scope>): … (KEY-N)`.

Push after GREEN, not after RED: CI runs the full unit suite on every push, and a pushed RED commit fails it.

## Where Tests Live

- Next to the code: `robothor/<module>/tests/test_<feature>.py`. Repo-wide gates: `tests/`.
- Dashboard: Vitest next to the component in `app/`; Playwright for user flows.
- Markers (`integration`, `llm`, `slow`, `e2e`, `smoke`) and their rules: `CONTRIBUTING.md`.

## Rules Specific to This Repo

- **Tests only ever touch a `*_test` database.** Never hardcode a production database name.
- **Patch the module the code imports *from*, not a re-export** (`CONTRIBUTING.md` explains the incident behind this).
- **Mock LLMs in unit tests.** Real calls only under `@pytest.mark.llm`. Assert properties, not exact model output.
- **Generic fixtures only:** Alice, Bob, `agent@example.com`, `test-tenant`. This repository is public.
- A bug fix starts with a test that reproduces the bug and fails on `main`.

## Prompting Anti-Patterns

| Don't say this                   | Say this instead                                                  |
| -------------------------------- | ----------------------------------------------------------------- |
| "Write tests for this feature"   | "Write FAILING tests for behaviour that doesn't exist yet"        |
| "Add tests and implementation"   | Two separate steps, two commits                                    |
| "Make sure tests pass"           | "Write tests first, then implement minimally"                     |
| "Test the `resolve_owner` function" | "Write tests for the owner-resolution behaviour, assuming no code exists" |

## Common Pitfalls

1. **Implementation leakage** — thinking ahead while writing tests. Fix: hard phase boundaries.
2. **Deleting or weakening tests to pass** — never. Review every change to a test file; you own the commit.
3. **Over-implementation** — features no test asked for. Fix: "only enough to pass the current tests".
4. **Skipping refactor** — duplication accumulates. Fix: an explicit refactor phase with its own commit.
5. **Testing implementation details** — tests coupled to internals. Fix: test behaviour and outcomes.
