# Contributing to JustPass (Private DeepTutor Fork)

This repository is maintained as a private, solo-development checkout. Keep
changes narrow, preserve unrelated work already present in the working tree,
and treat `main` as the integration branch.

## Working Model

- A feature branch is optional. Use one when it makes a risky or long-running
  change easier to isolate; direct work on `main` is otherwise allowed.
- A pull request is optional. Use one when an independent review or a durable
  review record is useful; routine work does not require one.
- Committing, pushing, opening or merging a pull request, releasing, and
  publishing are human decisions. Codex must not perform them without an
  explicit instruction.
- Do not discard, rewrite, stage, or include unrelated working-tree changes.

## Source Setup

Use Python `>=3.11,<3.14` and install from an authorized checkout. Public wheel
installation is not a supported workflow for the JustPass private fork.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

For the Web application, use the repository's existing Node.js toolchain and
lockfile:

```bash
cd web
npm ci --legacy-peer-deps
```

## Development and Verification

Make the smallest maintainable change that satisfies the task. Start with the
focused tests or checks closest to the changed behavior, then widen validation
only when the risk justifies it. Useful repository checks include:

```bash
pre-commit run --all-files
ruff check path/to/changed.py
ruff format --check path/to/changed.py
```

Run only commands supported by the current repository configuration. Report
which checks ran, which failed, and which important checks remain unrun.

To enable the optional dependency-free repository-hygiene hook in this
checkout:

```bash
git config core.hooksPath scripts/hooks
```

The hook rejects tracked generated or anomalous files. It does not restrict
direct commits to `main`.

Update documentation when public behavior, configuration, installation, or a
developer contract changes. Tests should cover regressions or important
contracts rather than increase test count for its own sake.

## Secrets and Private Data

- Never commit credentials, tokens, private keys, customer data, or raw private
  datasets.
- Run the configured `detect-secrets` pre-commit hook before integration.
- Inspect a finding without printing its value. Classify it by path and plugin
  or secret type.
- Stop and report a real secret; do not add it to the baseline.
- Add only a confirmed fixture, placeholder, or fixed hash to the existing
  baseline, and change only the relevant entry.
- Never regenerate or wholesale overwrite `.secrets.baseline`.

## Integration

Before integrating into `main`, review the diff for scope, accidental secrets,
debug artifacts, and unrelated changes. The human maintainer decides the final
commit message and whether to commit, push, request review, merge, release, or
deploy.
