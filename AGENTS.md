# AGENTS.md

Rules for coding agents working in this repo. Anything a tool can check is
checked by Ruff, mypy and pre-commit (config in `pyproject.toml` and
`.pre-commit-config.yaml`). This file covers what tools can't check.

## Commands

- Setup: `uv sync`
- Add a dependency: `uv add <pkg>` (dev: `uv add --dev <pkg>`). Never `pip install`, never edit `uv.lock` by hand.
- Run anything: `uv run <cmd>`
- Check everything: `uv run pre-commit run --all-files && uv run pytest`

## Definition of done

A task is done only when the full check above passes. When you report back:
- Say which commands you ran and whether they passed.
- If you could not run something, say so. Never claim a check passed that you didn't run.
- Keep the summary to a few lines. No emoji, no "production-ready", no restating the diff.

## Scope

- Change only what the task needs. No drive-by refactors, renames or reformatting of
  untouched code: they bury the real change in review.
- Don't add abstraction layers, base classes, factories, config flags or plugin hooks
  "for future use". Three similar lines beat a premature helper.
- Don't add backwards-compatibility shims, deprecation wrappers or re-exports unless
  asked. Nothing outside this repo depends on its internals.
- Don't add a dependency without asking. Prefer the standard library.
- Don't create new files (docs, examples, scripts, READMEs) unless asked.

## Don't fake progress

- No placeholder implementations, stub returns, mock data or `pass  # TODO` passed off as
  working code. If you're blocked, stop and say what's missing.
- No silent fallbacks: don't catch an error and return a default, empty list or `None`
  to keep things running. A crash we can see beats wrong data we can't.
- Never weaken, skip or delete a test to make it pass. If a test looks wrong, say why
  and ask.
- Never add `# noqa`, `# type: ignore` or per-file ignores to get past a check without
  saying so in your report. Each one needs an error code and a one-line reason.

## Errors

- Let exceptions propagate unless this layer can actually handle them. No `try/except`
  "to be safe".
- Don't add defensive checks for states the types already rule out (e.g. `None` checks
  on non-optional values).
- Catch the narrowest exception type. When re-raising, use `raise ... from err`.
- Log with the module logger (`logger = logging.getLogger(__name__)`), never `print`.
  Use `logger.exception` inside `except` blocks.

## Types

- Annotate everything. Avoid `Any`; prefer precise types, `TypedDict`, `Protocol` or
  generics. If `Any` is truly needed (e.g. raw JSON at a boundary), contain it there
  and parse into a typed structure straight away.
- Don't use `cast()` to silence the checker. Fix the types.
- Use dataclasses or Pydantic models for structured data, not loose dicts passed around.

## Comments and docstrings

- Comments explain *why*, never *what*. If the code needs a "what" comment, rename things.
- Don't narrate your changes in comments ("# Updated to fix X", "# New helper"). That
  belongs in the commit message.
- No section-banner comments, no ASCII art, no emoji or symbols like ✓ ✗ → in code.
- Docstrings on public functions only when the name and signature don't already say it
  all. No `Args:` blocks that repeat the parameter names.

## Tests

- Use pytest. Test behaviour through public interfaces, not implementation details.
- Mock only at real boundaries (network, clock, third-party APIs). Tests that only
  assert a mock was called prove nothing.
- Every bug fix gets a test that fails without the fix.
- Prefer a few clear tests over many near-duplicates; use `pytest.mark.parametrize`.

## Database (PostgreSQL)

- Always use parameterized queries. Never build SQL with f-strings or `%` formatting.
- Schema changes go through migrations. Never edit a migration that has been applied.
- Use proper column types (`timestamptz`, `numeric`, `uuid`, `jsonb` only where the
  shape really varies) rather than text for everything.

## Project-specific

- Project name: fledge (Python package `fledge`, in `src/fledge/`).
- Purpose: open source software factory.
