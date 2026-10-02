# Contributing to Asago Artifact Generator

Thank you for your interest in contributing. This document covers the
essentials for getting started.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
git clone <repo-url>
cd asago-artifact-generator
uv sync --locked
```

## Development workflow

```bash
./scripts/quality.sh          # ruff check + format check
uv run pytest tests/ -q       # unit tests
uv run pytest tests/ -q -v   # verbose
```

Run `./scripts/quality.sh` before pushing. CI enforces the same checks.

## Git hooks (optional)

To run `quality.sh` before each commit:

```bash
cat <<'EOF' > .git/hooks/pre-commit
#!/usr/bin/env bash
exec ./scripts/quality.sh
EOF
chmod +x .git/hooks/pre-commit
```

## Pull requests

- Branch from `main` and target `main` in PRs.
- Keep changes focused; one concern per PR.
- Update `README.md` and `CLAUDE.md` when interfaces or workflows change.
- `AGENTS.md` is a symlink to `CLAUDE.md`; edit `CLAUDE.md` only.

## License

By contributing, you agree that your contributions are licensed under the
Apache 2.0 License (see `LICENSE`).
