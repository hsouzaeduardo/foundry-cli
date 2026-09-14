# SPDX-License-Identifier: Apache-2.0
"""Terminal output.

``foundry`` is a launcher: it prints a handful of lines and then hands the
terminal to somebody else's CLI. So this module deliberately offers no panels,
tables, rules or progress bars -- if a helper existed to draw a box, a box would
end up on screen, and the agent's own first screenful would start halfway down.
Six line-shaped helpers and a spinner is the whole vocabulary.

Three behaviours are load-bearing rather than cosmetic:

*Streams.* Diagnostics (``warn``, ``fail``) and the spinner go to stderr;
everything else to stdout. ``foundry auth-token`` writes a bearer token to
stdout and is invoked by another program's credential command (agent reference
section 2), so anything chatty on stdout corrupts a token.

*Markup off.* Every message carries user data -- endpoints, ``az`` error text,
deployment names -- and a stray ``[`` in it would otherwise be parsed as a rich
style tag and swallowed.

*Encoding fallback.* On a legacy Windows console the stream encoding is a
codepage that cannot represent a check mark, and writing one raises
``UnicodeEncodeError``. Markers degrade to ASCII when the stream cannot take
them; the same class of failure was already measured on ``az`` output
(API notes, third pass).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any

from rich.console import Console
from rich.text import Text

#: Width the key column is padded to in :func:`kv`. Wide enough for
#: "resource group" and "subscription"; longer keys simply push their value out
#: rather than being truncated, since a truncated key is a lie.
_KEY_WIDTH = 15

_MARKERS: dict[str, tuple[str, str]] = {
    # name: (preferred, ascii fallback)
    "ok": ("✓", "+"),
    "warn": ("!", "!"),
    "fail": ("✗", "x"),
}

#: Set once anything has been printed, so the first :func:`section` does not
#: open the output with a blank line.
_printed = False


def section(title: str) -> None:
    """Start a group of related lines. A bold title, no rule, no box."""
    global _printed
    console = _out()
    if _printed:
        console.print()
    console.print(Text(title, style="bold"))
    _printed = True


def kv(key: str, value: str) -> None:
    """One indented ``key   value`` line, the body of a ``section``."""
    line = Text("  ")
    line.append(f"{key}".ljust(_KEY_WIDTH), style="dim")
    line.append(str(value))
    _write(_out(), line)


def ok(msg: str) -> None:
    """Something succeeded."""
    _marked(_out(), "ok", msg, "green")


def warn(msg: str) -> None:
    """Something is off but the run continues. Goes to stderr."""
    _marked(_err(), "warn", msg, "yellow")


def fail(msg: str) -> None:
    """Something failed. Goes to stderr.

    Printing does not exit -- the exit code is the caller's decision (SPEC
    section 7: 1 for user-fixable, 2 for internal).
    """
    _marked(_err(), "fail", msg, "red")


def note(msg: str) -> None:
    """Secondary detail: the remedy under a failure, a path just written."""
    _write(_out(), Text(f"  {msg}", style="dim"))


class Spinner:
    """Handle yielded by :func:`spinner`; ``update`` retitles a live spinner.

    On a non-TTY there is nothing to retitle, so ``update`` is a no-op rather
    than a second line of noise in a log file.
    """

    __slots__ = ("_status",)

    def __init__(self, status: Any | None) -> None:
        self._status = status

    def update(self, msg: str) -> None:
        if self._status is not None:
            self._status.update(Text(msg))


@contextmanager
def spinner(msg: str) -> Iterator[Spinner]:
    """Animate ``msg`` while a slow call runs (a subscription scan, ``az``).

    Degrades to a single plain line when stderr is not a TTY, so piped or
    redirected output never accumulates escape sequences. Always tears the live
    display down, including on exception -- otherwise a traceback would print on
    top of a running animation.
    """
    global _printed
    console = _err()
    if not console.is_terminal or console.is_dumb_terminal:
        _write(console, Text(f"{msg}...", style="dim"))
        yield Spinner(None)
        return

    _printed = True
    with console.status(Text(msg), spinner="dots") as status:
        yield Spinner(status)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _no_color() -> bool:
    """Honour https://no-color.org: any non-empty ``NO_COLOR`` disables colour."""
    return bool(os.environ.get("NO_COLOR"))


def _console(stream: IO[str]) -> Console:
    """A fresh Console per call.

    Cheap, and it means ``NO_COLOR`` and a reassigned ``sys.stdout`` (pytest's
    capture, a subprocess wrapper) are always observed as they are now rather
    than as they were at import time.
    """
    no_color = _no_color()
    return Console(
        file=stream,
        no_color=no_color,
        color_system=None if no_color else "auto",
        markup=False,
        highlight=False,
        emoji=False,
        soft_wrap=True,
    )


def _out() -> Console:
    return _console(sys.stdout)


def _err() -> Console:
    return _console(sys.stderr)


def _marker(console: Console, name: str) -> str:
    preferred, fallback = _MARKERS[name]
    encoding = getattr(console.file, "encoding", None) or "ascii"
    try:
        preferred.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return fallback
    return preferred


def _marked(console: Console, name: str, msg: str, style: str) -> None:
    line = Text()
    line.append(f"{_marker(console, name)} ", style=style)
    line.append(str(msg))
    _write(console, line)


def _write(console: Console, text: Text) -> None:
    global _printed
    console.print(text)
    _printed = True
