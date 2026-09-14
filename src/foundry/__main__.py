# SPDX-License-Identifier: Apache-2.0
"""``python -m foundry``.

Exists so a source checkout, a virtualenv whose ``Scripts``/``bin`` directory is
not on PATH, or a machine where the console script was shadowed can still run the
CLI -- and so the documented invocation is identical to the installed one.

The work is delegated to :func:`foundry.cli.main`, which owns the exit codes; a
second copy of that mapping here would drift.
"""

from __future__ import annotations

from foundry.cli import main

if __name__ == "__main__":
    main()
