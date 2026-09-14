# Provenance

This document records where this codebase came from, so the question can be
answered from evidence rather than from memory.

## Summary

`foundry` was written from a functional specification, by contributors who were
procedurally barred from reading any prior implementation of a similar tool. Its
two factual inputs were:

1. **Direct measurement of the Microsoft Foundry API** against a live Azure
   subscription — recorded in `docs/foundry-api-notes.md`. Every endpoint shape,
   header requirement, token lifetime and error behaviour in that file was
   observed, not assumed.
2. **The public vendor documentation of each coding-agent CLI** — recorded in
   `docs/agent-config-reference.md`, with a source URL for each claim, plus the
   `--help` output of the installed CLIs themselves.

Both are facts about third-party public interfaces. Neither is anyone's
proprietary expression.

## Why this matters

A tool that points several coding-agent CLIs at a hosted model gateway is not a
novel idea, and at least one prior implementation exists under a licence that
restricts use to a specific vendor's services. Copyright protects the
**expression** of a program — including its structure, sequence and organisation —
not the **idea** of what it does. A project intended for open-source release
therefore has to be able to show that its expression is its own.

## Procedure

- The specification (`docs/SPEC.md`) was authored first and is the sole design
  input. It states requirements and observed API behaviour; it does not describe
  any prior implementation's code.
- Implementation was carried out under an explicit constraint prohibiting reading,
  opening, listing, searching or referencing any other codebase. Where a
  contributor wanted to know "how this was done before", the answer was that they
  could not look, and had to design it from the specification.
- The architecture in `docs/SPEC.md` §10 was chosen for this specification's
  requirements: an authentication module over the Azure CLI, an ARM discovery
  module, endpoint construction, a state file, and one self-contained module per
  agent behind a small protocol.

## Measured independence

Measured 2026-08-24 against the **pristine prior implementation**, extracted from
its own version control at the commit before any modification. (An earlier draft
of this document compared against a *modified* copy of that tree and consequently
overstated the overlap; the figures below are the correct comparison.)

| Metric | Prior implementation | This tree | In common |
|---|---|---|---|
| Modules | 36 | 14 | 7 |
| Functions | 719 | 211 | **12** |
| Classes | 14 | 19 | **0** |

**No class name is shared with the prior implementation.**

The 7 shared module names are `__init__.py`, `cli.py`, and one file per supported
agent (`claude.py`, `codex.py`, `copilot.py`, `opencode.py`, `pi.py`). There is no
alternative name for the module that adapts Codex.

The 12 shared function names are, in full: `__init__`, `main`, `_run`,
`_version_callback`, `configure`, `default_model`, `launch`, `revert`, `spinner`,
`status`, `stop`, `token`. Every one is either a Python convention, the framework's
own documented idiom, or the obvious verb for the operation. Of the twelve, only
three have identical signatures, and each of those is the only form it could take:
`main()`, `status()`, and `_version_callback(value)` — the last being the pattern
published in Typer's own documentation for implementing `--version`.

Names of this kind are *scènes à faire*: dictated by the task, the language and the
framework rather than chosen. They were deliberately **not** renamed to manufacture
difference. Renaming `launch()` to something less natural would degrade the code
without affecting the analysis, since substance rather than labelling is what an
independence assessment turns on.

A line-level comparison was also run. Restricted to significant lines (ignoring
blanks, comments, imports, decorators and lines under 25 characters), shared lines
consist of third-party constant names (`COPILOT_PROVIDER_API_KEY`,
`ARM_RESOURCE = "https://management.azure.com"`), standard Python idioms
(`except json.JSONDecodeError:`, `if __name__ == "__main__":`), documented Typer
boilerplate (`context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`),
and single statements with one obvious form (`args += ["--subscription", subscription]`) —
facts and idiom, not expression.

## Known limitation, stated plainly

The author of `docs/SPEC.md` had previously read the prior implementation. The
specification was written from the two factual sources above and describes
requirements rather than code, but its **scope decisions** — which agents to
support, which features to leave out — were informed by that familiarity. This is
disclosed rather than glossed over.

The mitigation is that scope is not expression: knowing that a tool ought to
support five agent CLIs and leave out usage reporting is not a protectable
element. The implementation itself, which is the expression, was produced under
the no-reading constraint and measures as independent above.

If a stricter standard is required, the remedy is a review of this tree by someone
who has never seen the prior implementation, against `docs/SPEC.md` alone.

## Reproducing the measurement

The comparison normalises each source line (strip, collapse whitespace), discards
blanks, comments, `import`/`from` statements, decorators, bare `return`/`pass`,
and any line under 25 characters, then intersects the two sets. Symbol comparison
parses each tree with `ast` and intersects the sets of function and class names.

## Licence

Apache-2.0 (`LICENSE`). Every source file carries
`# SPDX-License-Identifier: Apache-2.0`.
