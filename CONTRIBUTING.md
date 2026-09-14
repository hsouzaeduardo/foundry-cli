# Contributing to foundry

`foundry` is a launcher, not a platform — see `docs/SPEC.md` for what that means
and what is explicitly out of scope. Read it before proposing a feature; it
saves everyone a round-trip on pull requests that add things v1 deliberately
excludes (MCP registration, telemetry, model routing, and so on).

## Setup

```bash
git clone https://github.com/hsouzaeduardo/foundry-cli.git
cd foundry-cli
uv sync
```

Run the CLI from the checkout:

```bash
uv run foundry --version
```

## Before opening a pull request

```bash
uv run pytest
uv run ruff check .
```

Both must pass. There is no CI yet, so this is the only gate — treat it as if
there were.

- Unit tests mock at the subprocess and HTTP boundary. No test may require a
  real `az` binary or network access.
- Assertions are exact — check the exact bytes an agent module writes and the
  exact argv it execs, not just that "something" was written.
- New behaviour needs a new test. A bug fix should include a test that fails
  without the fix.

## Adding support for a new agent CLI

This is the contribution this project is built to make easy. It means:

1. One new module under `src/foundry/agents/`, satisfying the protocol in
   `agents/base.py`: `name`, `binary`, `is_installed()`,
   `configure(profile, deployment)`, `launch(profile, args)`,
   `revert(profile)`.
2. One line registering it.
3. A short section in `docs/agent-config-reference.md` citing the agent's own
   documentation for the config keys, file locations, and environment
   variables you relied on — this project does not guess at another vendor's
   config format, it cites it.

Before writing to a shared config file, check whether the agent has an
environment variable that relocates its config root entirely (see SPEC §6,
policy 1). Isolating `foundry`'s writes under `~/.foundry/` is strongly
preferred over editing a file the user also edits by hand, and it makes
`foundry revert` a directory delete instead of a surgical key removal.

## Style

- Every source file starts with `# SPDX-License-Identifier: Apache-2.0`.
- `ruff` (line length 100) is the formatter and linter; run it before
  committing rather than fixing findings after the fact.
- Match the existing tone in docstrings and comments: state the *why*, not
  the *what* — the code already says what it does.

## Reporting bugs

Include your OS, the agent CLI and version involved, and — if the bug is in
model discovery or endpoint handling — the output of `foundry status`. Redact
your resource name and subscription ID if you'd rather not share them.

## Licence

By contributing, you agree your contribution is licensed under Apache-2.0,
the same as the rest of the project.
