# SPDX-License-Identifier: Apache-2.0
"""The command surface (SPEC section 7).

Four properties of this module are load-bearing; everything else is plumbing.

*Passthrough is total.* ``foundry claude -r`` must reach Claude Code as ``-r``.
Every launch command therefore runs with ``ignore_unknown_options`` and
``allow_extra_args``, and forwards ``ctx.args`` verbatim and in order. Only
``--model`` and ``--endpoint`` are consumed, because SPEC section 7 names them;
``--`` ends foundry's parsing for anything that would otherwise collide.

*The agent replaces us.* On POSIX the launch is ``os.execvpe``, so no foundry
process lingers to mangle signals, job control or the exit status. Windows has
no ``exec``, so the child is spawned, ``SIGINT`` is ignored in the parent for the
child's lifetime -- Ctrl+C reaches every process attached to a Windows console,
and a parent that died first would hand the shell back while the agent was still
drawing -- and the child's exit code is forwarded unchanged.

*stdout belongs to somebody else.* ``foundry auth-token`` is invoked by Codex's
credential command (agent reference section 2) and its stdout is parsed as a
bearer token, so that path prints the token and nothing else. Every diagnostic in
this module goes to stderr through :mod:`foundry.console`.

*The agent modules load lazily.* Importing ``foundry.agents.*`` at module scope
would put five module imports and their filesystem probing in front of
``foundry --version`` and in front of the token mint Codex re-runs every fifteen
minutes.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer

from foundry import __version__, auth, console, discovery, endpoints, profile

if TYPE_CHECKING:  # pragma: no cover - only needed for annotations
    from foundry.agents.base import Agent
    from foundry.profile import Profile

#: The launch commands, in the order they appear in help and in ``status``.
AGENT_ORDER: tuple[str, ...] = ("claude", "codex", "copilot", "opencode", "pi")

#: SPEC section 7. 1 is "you can fix this"; 2 is "we have a bug".
EXIT_OK = 0
EXIT_USER = 1
EXIT_INTERNAL = 2

#: Ctrl+C. Not in SPEC, but 130 is what a shell expects from an interrupted
#: command, and reporting 1 would make a cancelled configure look like a failure.
EXIT_INTERRUPTED = 130

#: Set to anything non-empty to get the traceback behind an exit-2.
DEBUG_ENV = "FOUNDRY_DEBUG"

#: Install commands for the agents whose published package name is known. An
#: agent module that defines ``install_hint`` overrides this -- it knows its own
#: vendor better than a table here does.
_INSTALL_HINTS: dict[str, str] = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
    "copilot": "npm install -g @github/copilot",
    "opencode": "npm install -g opencode-ai",
}

#: Attributes that make an object usable as an ``agents.base.Agent``.
_AGENT_ATTRS: tuple[str, ...] = (
    "name",
    "binary",
    "display",
    "is_installed",
    "default_model",
    "configure",
    "launch_env",
    "launch_argv",
)

#: How many OpenAI deployment names ``status`` prints before summarising.
_STATUS_LIST_LIMIT = 6

#: Longest file body ``configure --dry-run`` prints in full.
_DRY_RUN_BODY_LIMIT = 8000

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|apikey|token|bearer|secret|password|credential)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}\]]{8,})",
    re.IGNORECASE,
)
#: Environment variable names whose *value* is a credential, e.g.
#: ``COPILOT_PROVIDER_API_KEY``, ``ANTHROPIC_FOUNDRY_AUTH_TOKEN``.
_SECRET_ENV_RE = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|BEARER)", re.IGNORECASE)


class UserError(RuntimeError):
    """A user-fixable failure: exit 1, and the message names the concrete fix."""


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Launch coding-agent CLIs against a Microsoft Foundry endpoint.",
)


# --------------------------------------------------------------------------- #
# Root                                                                          #
# --------------------------------------------------------------------------- #


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"foundry {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(  # noqa: B008 - typer declares options this way
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Point Claude Code, Codex, Copilot CLI, OpenCode and Pi at Microsoft Foundry.

    Run `az login`, then `foundry claude`. Everything after the agent name is
    handed to the agent untouched.
    """


# --------------------------------------------------------------------------- #
# Launch                                                                        #
# --------------------------------------------------------------------------- #


def _make_launch_command(tool: str) -> None:
    """Register one launch command.

    A factory because all five differ only in a string, and five hand-copied
    bodies would be five places for a passthrough bug to hide.
    """

    def launch(
        ctx: typer.Context,
        model: str = typer.Option(  # noqa: B008 - typer declares options this way
            None,
            "--model",
            metavar="DEPLOYMENT",
            help="Launch with a specific deployment name.",
        ),
        endpoint: str = typer.Option(  # noqa: B008 - typer declares options this way
            None,
            "--endpoint",
            metavar="URL",
            help="Use (and if needed set up) a different Foundry endpoint.",
        ),
    ) -> NoReturn:
        _launch(tool, list(ctx.args), model=model, endpoint=endpoint)

    launch.__name__ = tool
    launch.__doc__ = (
        f"Launch {tool}, configuring it first if necessary.\n\n"
        "Unrecognised arguments pass straight through. Put `--` before anything "
        f"that would otherwise be read as one of foundry's own: `foundry {tool} -- --help`."
    )
    app.command(
        name=tool,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(launch)


for _tool in AGENT_ORDER:
    _make_launch_command(_tool)
del _tool


def _launch(tool: str, args: list[str], *, model: str | None, endpoint: str | None) -> NoReturn:
    """Configure if needed, then hand the terminal to the agent."""
    agent = _load_agent(tool)
    if not _is_installed(agent):
        raise UserError(_missing_binary_message(agent))

    prof = _profile_for_launch(agent, endpoint=endpoint)
    chosen = _resolve_model(agent, prof, model)

    env = {**auth.scrubbed_env(), **_agent_env(agent, prof, chosen)}
    argv = _agent_argv(agent, prof, args)
    _exec(argv, env, agent)


def _profile_for_launch(agent: Agent, *, endpoint: str | None) -> Profile:
    """Return a profile with *agent* configured, building one if necessary."""
    prof = profile.load()
    wanted = _normalize(endpoint) if endpoint is not None else None

    if prof is None:
        prof = _build_profile(wanted, None)
        _configure_agents(prof, [agent])
        profile.save(prof)
        return prof

    if wanted is not None and wanted != prof.endpoint:
        # A different endpoint invalidates every config foundry has written: the
        # base URLs and the deployment names both change. Dropping the records
        # makes the other agents rebuild on their next launch instead of quietly
        # talking to a resource that is no longer the configured one.
        prof = _build_profile(wanted, prof.subscription or None)
        prof.agents = {}
        _configure_agents(prof, [agent])
        profile.save(prof)
        return prof

    if not prof.is_configured(agent.name):
        _configure_agents(prof, [agent])
        profile.save(prof)
    return prof


def _resolve_model(agent: Agent, prof: Profile, requested: str | None) -> str | None:
    """Validate ``--model``, or fall back to the agent's own default.

    ``None`` is a legitimate answer: Claude Code's native Foundry mode pins three
    deployment names through the environment and has no single "the model".
    """
    known = prof.all_deployments()
    if not known:
        raise UserError(
            f"No chat deployment was found on {prof.account or prof.endpoint}.\n"
            "  Publish one in the Microsoft Foundry portal (https://ai.azure.com), then run:\n"
            "    foundry configure"
        )

    if requested:
        if requested in known:
            return requested
        raise UserError(
            f"'{requested}' is not a deployment on {prof.account or prof.endpoint}.\n"
            f"  Deployments foundry knows about: {', '.join(known)}\n"
            "  If you published it after the last scan, refresh with:\n"
            f"    foundry configure --endpoint {prof.endpoint}"
        )

    try:
        return agent.default_model(prof)
    except Exception as exc:  # an agent bug must not read as a foundry crash
        raise UserError(
            f"{_display(agent)} could not choose a default deployment: {exc}\n"
            f"  Pick one explicitly: foundry {agent.name} --model {known[0]}"
        ) from exc


def _agent_env(agent: Agent, prof: Profile, model: str | None) -> dict[str, str]:
    """The agent's own environment contribution.

    Merged *over* :func:`auth.scrubbed_env`, which is right whether the agent
    returns only its additions or a whole environment: the scrub removes a stale
    service principal (agent reference section 1), and a superset merge over an
    already-scrubbed base cannot put one back.
    """
    try:
        env = agent.launch_env(prof, model)
    except (auth.AuthError, UserError):
        raise
    except Exception as exc:
        raise UserError(
            f"{_display(agent)} could not build its launch environment: {exc}\n"
            "  Re-run `foundry configure` to rebuild its configuration."
        ) from exc
    return {str(k): str(v) for k, v in dict(env).items()}


def _agent_argv(agent: Agent, prof: Profile, args: list[str]) -> list[str]:
    """The agent's argv, with the caller's passthrough arguments already in it."""
    try:
        argv = [str(a) for a in agent.launch_argv(prof, list(args))]
    except (auth.AuthError, UserError):
        raise
    except Exception as exc:
        raise UserError(
            f"{_display(agent)} could not build its command line: {exc}\n"
            "  Re-run `foundry configure` to rebuild its configuration."
        ) from exc
    if not argv:
        raise RuntimeError(f"{agent.name}.launch_argv returned an empty command line")
    return argv


def _exec(argv: list[str], env: dict[str, str], agent: Agent) -> NoReturn:
    """Replace this process with the agent (POSIX), or shepherd it (Windows)."""
    program = shutil.which(argv[0])
    if program is None:
        if os.path.isabs(argv[0]) and os.path.exists(argv[0]):
            program = argv[0]
        else:
            raise UserError(_missing_binary_message(agent))

    if os.name != "nt":
        try:
            # argv[0] stays the plain name so the agent reports itself normally.
            os.execvpe(program, [argv[0], *argv[1:]], env)
        except OSError as exc:
            raise UserError(f"{_missing_binary_message(agent)}\n  (exec said: {exc})") from exc
        raise AssertionError("os.execvpe returned")  # pragma: no cover

    raise SystemExit(_spawn_windows([program, *argv[1:]], env, agent))


def _spawn_windows(argv: list[str], env: dict[str, str], agent: Agent) -> int:
    """Run the agent as a child process and return its exit code.

    Ctrl+C in a Windows console is delivered to every attached process, so the
    parent ignores SIGINT for the child's lifetime rather than racing it to the
    exit; otherwise the shell redraws its prompt on top of a still-running agent.
    """
    restore = _ignore_sigint()
    try:
        proc = subprocess.Popen(argv, env=env)
    except FileNotFoundError as exc:
        restore()
        raise UserError(_missing_binary_message(agent)) from exc
    except OSError as exc:
        restore()
        raise UserError(
            f"Could not start {_display(agent)} ({argv[0]}): {exc}\n"
            f"  Check that `{agent.binary}` runs on its own."
        ) from exc

    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:  # pragma: no cover - the child owns Ctrl+C
                continue
    finally:
        restore()


def _ignore_sigint() -> Any:
    """Ignore SIGINT and return a callable that puts the old handler back."""
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        return lambda: None

    def restore() -> None:
        try:
            signal.signal(signal.SIGINT, previous)
        except (ValueError, OSError):  # pragma: no cover
            pass

    return restore


# --------------------------------------------------------------------------- #
# configure                                                                     #
# --------------------------------------------------------------------------- #


@app.command()
def configure(
    endpoint: str = typer.Option(  # noqa: B008 - typer declares options this way
        None,
        "--endpoint",
        metavar="URL",
        help="Resource name or endpoint URL. Omit to pick from what is visible.",
    ),
    subscription: str = typer.Option(  # noqa: B008 - typer declares options this way
        None,
        "--subscription",
        metavar="ID",
        help="Search only this subscription. Use it when the scan is slow or ambiguous.",
    ),
    agents: str = typer.Option(  # noqa: B008 - typer declares options this way
        None,
        "--agents",
        metavar="a,b",
        help=f"Comma-separated agents to configure ({', '.join(AGENT_ORDER)}), or 'all'.",
    ),
    dry_run: bool = typer.Option(  # noqa: B008 - typer declares options this way
        False,
        "--dry-run",
        help="Print exactly what would be written, and write nothing.",
    ),
) -> None:
    """Set up the Foundry endpoint and configure the agents."""
    _require_signin()

    resolved = _resolve_endpoint(endpoint, subscription)
    selected = _select_agents(_parse_agent_list(agents))

    if dry_run:
        _configure_dry_run(resolved, subscription, selected)
        return

    prof = _build_profile(resolved, subscription)
    _configure_agents(prof, selected)
    profile.save(prof)

    console.section("Configured")
    console.kv("endpoint", prof.endpoint)
    console.kv("resource", f"{prof.account} ({prof.resource_group})")
    for line in _deployment_lines(prof):
        console.kv(*line)
    if selected:
        console.kv("agents", ", ".join(a.name for a in selected))
        console.note(f"their configuration lives under {_short(profile.app_dir() / 'agents')}")
    console.ok(f"Ready. Run: foundry {selected[0].name if selected else 'claude'}")


def _configure_agents(prof: Profile, selected: list[Agent]) -> None:
    """Run each agent's ``configure`` and record it in the profile.

    The record is written here, and only when the agent did not write one itself,
    so ``status`` and ``revert`` see the same bookkeeping for all five regardless
    of how each module chose to behave.
    """
    for agent in selected:
        if not _is_installed(agent):
            console.warn(f"{_display(agent)} is not on PATH; configuring it anyway.")
            console.note(_install_hint(agent))
        with console.spinner(f"Configuring {_display(agent)}"):
            try:
                agent.configure(prof, None)
            except (auth.AuthError, UserError, profile.ProfileError):
                raise
            except Exception as exc:
                home = profile.app_dir() / "agents" / agent.name
                raise UserError(
                    f"Could not configure {_display(agent)}: {exc}\n"
                    f"  Check that {_short(home)} is writable."
                ) from exc
        if not prof.is_configured(agent.name):
            profile.record_agent(prof, agent.name, home=profile.agent_home(agent.name))


def _configure_dry_run(endpoint: str, subscription: str | None, selected: list[Agent]) -> None:
    """Do the whole configure against a throwaway home, then print the result.

    Printing what *would* be written is only honest if something actually writes
    it, so the run is real and only the destination is fake. ``FOUNDRY_HOME``
    relocates ``~/.foundry``, and every agent is confined to a directory under it
    by its own isolation variable (agent reference section 6), so nothing escapes
    the temporary tree.
    """
    real_home = profile.app_dir()
    previous = os.environ.get(profile.HOME_ENV)

    with tempfile.TemporaryDirectory(prefix="foundry-dry-run-") as tmp:
        os.environ[profile.HOME_ENV] = tmp
        try:
            prof = _build_profile(endpoint, subscription)
            _configure_agents(prof, selected)
            profile.save(prof)
            previews = {a.name: _launch_env_preview(a, prof) for a in selected}
            files = _collect_files(Path(tmp))
        finally:
            if previous is None:
                os.environ.pop(profile.HOME_ENV, None)
            else:
                os.environ[profile.HOME_ENV] = previous

        rehome = _rehomer(tmp, real_home)

        console.section("Would write")
        if not files:
            console.note("no files")
        for path in files:
            console.kv("file", str(real_home / path.relative_to(tmp)))
            for line in _body_lines(path, rehome):
                console.note(f"  {line}")

        for name, env in previews.items():
            if not env:
                continue
            console.section(f"Would launch {name} with")
            for key in sorted(env):
                console.kv(key, rehome(env[key]))

    console.section("Dry run")
    console.ok("Nothing was written.")
    console.note("credential values above are shown as <redacted>")


def _rehomer(tmp: str, real_home: Path) -> Any:
    """Rewrite the throwaway home back to ``~/.foundry`` in printed text.

    The agents write their own absolute paths into their config (``CODEX_HOME``
    and friends), so without this the dry run would print paths that no real run
    would ever produce -- which is precisely the promise the flag makes. The
    JSON-escaped spelling is substituted too, because a Windows path inside a
    JSON string arrives with doubled backslashes.
    """
    scratch, real = str(Path(tmp)), str(real_home)
    pairs = [(scratch, real), (scratch.replace("\\", "\\\\"), real.replace("\\", "\\\\"))]

    def rewrite(text: str) -> str:
        for old, new in pairs:
            if old != new:
                text = text.replace(old, new)
        return text

    return rewrite


def _launch_env_preview(agent: Agent, prof: Profile) -> dict[str, str]:
    """The variables a launch would add or override, redacted.

    Copilot configures its whole provider through the environment (agent
    reference section 3), so a dry run that printed only files would show nothing
    at all for it.
    """
    try:
        env = _agent_env(agent, prof, agent.default_model(prof))
    except Exception as exc:  # a preview must never be the thing that fails
        console.warn(f"could not preview {agent.name}'s environment: {exc}")
        return {}
    return {
        key: _redact_env(key, value)
        for key, value in env.items()
        if os.environ.get(key) != value and key not in auth.SP_ENV_VARS
    }


def _collect_files(root: Path) -> list[Path]:
    return sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: str(p).lower())


def _body_lines(path: Path, rehome: Any) -> list[str]:
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["<not a text file>"]
    if len(body) > _DRY_RUN_BODY_LIMIT:
        body = body[:_DRY_RUN_BODY_LIMIT] + "\n... (truncated)"
    return rehome(_redact(body)).splitlines() or ["<empty>"]


def _redact(text: str) -> str:
    """Blank out anything token-shaped.

    OpenCode and Pi embed a bearer directly in their config (agent reference
    sections 4 and 5), so an unredacted dry run would paste a live Azure token
    into a terminal, a screenshot or a CI log.
    """
    redacted = _JWT_RE.sub("<redacted-token>", text)
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}<redacted>", redacted)


def _redact_env(key: str, value: str) -> str:
    """Redact an environment value by the name it is bound to.

    A bare value carries no ``key = `` context for :func:`_redact` to match on,
    and Copilot's credential is exactly that: a bare value in
    ``COPILOT_PROVIDER_API_KEY`` (agent reference section 3).
    """
    if value and _SECRET_ENV_RE.search(key):
        return "<redacted>"
    return _redact(value)


# --------------------------------------------------------------------------- #
# status                                                                        #
# --------------------------------------------------------------------------- #


@app.command()
def status() -> None:
    """Show what is configured, and against what."""
    prof = profile.load()
    if prof is None:
        raise UserError("foundry is not configured yet.\n  Run: az login && foundry configure")

    console.section("Endpoint")
    console.kv("endpoint", prof.endpoint)
    console.kv("anthropic", endpoints.anthropic_base(prof.endpoint))
    console.kv("openai", endpoints.openai_base(prof.endpoint))

    console.section("Azure")
    console.kv("subscription", prof.subscription or "-")
    console.kv("resource group", prof.resource_group or "-")
    console.kv("account", prof.account or "-")
    identity = _signed_in_as()
    if identity:
        console.kv("signed in as", identity)

    console.section("Deployments")
    lines = _deployment_lines(prof)
    if lines:
        for line in lines:
            console.kv(*line)
    else:
        console.note("none recorded -- run `foundry configure` to rediscover")

    console.section("Agents")
    for tool in AGENT_ORDER:
        console.kv(tool, _agent_status(prof, tool))


def _deployment_lines(prof: Profile) -> list[tuple[str, str]]:
    """The deployments grouped by family, one line per family, newest first."""
    lines: list[tuple[str, str]] = []
    for family in ("opus", "sonnet", "haiku"):
        name = prof.anthropic(family)
        if name:
            lines.append((family, name))
    names = prof.openai()
    if names:
        shown = names[:_STATUS_LIST_LIMIT]
        extra = len(names) - len(shown)
        lines.append(("openai", ", ".join(shown) + (f", +{extra} more" if extra else "")))
    return lines


def _agent_status(prof: Profile, tool: str) -> str:
    record = prof.agents.get(tool)
    if not isinstance(record, dict):
        return "not configured"
    parts = ["configured"]
    when = record.get("configured_at")
    if isinstance(when, str) and when:
        parts.append(when)
    home = record.get("home")
    if isinstance(home, str) and home:
        parts.append(_short(Path(home)))
    return "  ".join(parts)


def _signed_in_as() -> str | None:
    """The active ``az`` identity, or ``None`` if it cannot be read.

    Best effort on purpose: ``status`` must still describe the recorded profile
    on a machine with no ``az``, no network and no session.
    """
    try:
        active = auth.account()
    except auth.AuthError:
        return None
    if not isinstance(active, dict):
        return None
    user = active.get("user")
    who = user.get("name") if isinstance(user, dict) else None
    return str(who) if who else (str(active.get("name") or "") or None)


# --------------------------------------------------------------------------- #
# revert                                                                        #
# --------------------------------------------------------------------------- #


@app.command()
def revert(
    yes: bool = typer.Option(  # noqa: B008 - typer declares options this way
        False,
        "--yes",
        "-y",
        help="Do not ask for confirmation.",
    ),
) -> None:
    """Undo every change foundry made."""
    base = profile.app_dir()
    if not base.exists():
        console.ok(f"Nothing to remove: {_short(base)} does not exist.")
        return

    targets = _removable(base)
    if not targets:
        console.ok(f"Nothing to remove: {_short(base)} holds no foundry state.")
        return

    console.section(f"Will remove from {_short(base)}")
    for label, path in targets:
        console.kv(label, _short(path))

    if not yes and _interactive() and not _confirm("Remove it?", default=True):
        console.warn("Nothing was removed.")
        raise typer.Exit(EXIT_OK)

    prof = profile.load()
    if prof is not None:
        _run_agent_reverts(prof)

    backups = base / "backups"
    keep_backups = backups.is_dir() and any(backups.iterdir())
    profile.clear(keep_backups=keep_backups)

    console.section("Removed")
    for label, path in targets:
        if label == "backups" and keep_backups:
            continue
        console.kv(label, _short(path))

    console.ok("foundry state removed.")
    console.note(
        "Every agent foundry configured lived in its own home directory under "
        f"{_short(base)}, reached only through that agent's own isolation variable, "
        "so deleting it is the complete undo."
    )
    console.note(
        "Nothing of yours was touched: ~/.claude, ~/.codex, ~/.copilot, "
        "~/.config/opencode and Pi's directory were never read or written."
    )
    if keep_backups:
        console.warn(f"Kept {_short(backups)}: it holds the only copy of a file foundry edited.")
        console.note("Restore from there if you need to, then delete the directory by hand.")


def _removable(base: Path) -> list[tuple[str, Path]]:
    """What ``revert`` is about to delete, in the order it is reported."""
    found: list[tuple[str, Path]] = []
    config = base / profile.CONFIG_NAME
    if config.exists():
        found.append(("config", config))
    agents_dir = base / "agents"
    if agents_dir.is_dir():
        found += [(child.name, child) for child in sorted(agents_dir.iterdir()) if child.is_dir()]
    backups = base / "backups"
    if backups.is_dir() and any(backups.iterdir()):
        found.append(("backups", backups))
    return found


def _run_agent_reverts(prof: Profile) -> None:
    """Give any agent module that implements ``revert`` a chance to run first.

    SPEC section 10 lists ``revert(profile)`` on the agent modules, but the
    isolation variables mean none of them should need it. Calling it when it
    exists costs nothing and covers a module that fell back to editing a shared
    file; not calling it would leave that edit behind.
    """
    for tool in AGENT_ORDER:
        try:
            agent = _load_agent(tool)
        except UserError:
            continue
        undo = getattr(agent, "revert", None)
        if not callable(undo):
            continue
        try:
            undo(prof)
        except Exception as exc:
            console.warn(f"{tool}: its own revert step failed ({exc}); continuing.")


# --------------------------------------------------------------------------- #
# auth-token (hidden)                                                           #
# --------------------------------------------------------------------------- #


@app.command(hidden=True)
def auth_token(
    endpoint: str = typer.Option(  # noqa: B008 - typer declares options this way
        None,
        "--endpoint",
        metavar="URL",
        help="Endpoint the token is for. Used only to pick the subscription.",
    ),
    subscription: str = typer.Option(  # noqa: B008 - typer declares options this way
        None,
        "--subscription",
        metavar="ID",
        help="Mint the token against this subscription.",
    ),
) -> None:
    """Print a bearer token to stdout. Not for humans.

    Codex re-runs this every ``refresh_interval_ms`` and reads stdout as the
    credential (agent reference section 2), so this command writes the token and
    absolutely nothing else; failures go to stderr and exit 1.
    """
    sub = subscription
    if sub is None:
        prof = profile.load()
        if prof is not None and prof.subscription:
            if endpoint is None or _normalize(endpoint) == prof.endpoint:
                sub = prof.subscription

    sys.stdout.write(auth.token(subscription=sub) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Endpoint and agent selection                                                  #
# --------------------------------------------------------------------------- #


def _resolve_endpoint(endpoint: str | None, subscription: str | None) -> str:
    """Decide which endpoint to configure against.

    An explicit ``--endpoint`` short-circuits the subscription scan entirely --
    that is the whole point of the flag every "not found" message advertises.
    """
    if endpoint:
        return _normalize(endpoint)

    accounts, report = _scan_accounts(subscription)
    if not accounts:
        raise UserError(_no_accounts_message(report, subscription))

    if len(accounts) == 1:
        only = accounts[0]
        if not _interactive() or _confirm(f"Use {_label(only)}?", default=True):
            return only.endpoint
        return _normalize(_ask_endpoint())

    if not _interactive():
        listed = "\n".join(f"  - {_label(a)}" for a in accounts)
        raise UserError(
            f"{len(accounts)} Foundry resources are visible and no endpoint was given.\n"
            f"{listed}\n"
            "  Pick one: foundry configure --endpoint <resource-or-url>"
        )

    choices = [_label(a) for a in accounts]
    other = "Other (type an endpoint)"
    picked = _ask_select("Which Foundry resource?", [*choices, other])
    if picked == other:
        return _normalize(_ask_endpoint())
    return accounts[choices.index(picked)].endpoint


def _label(account: discovery.Account) -> str:
    location = f" ({account.location})" if account.location else ""
    return f"{account.name}{location} -- {account.endpoint}"


def _no_accounts_message(report: Any, subscription: str | None) -> str:
    """SPEC section 9: name which subscriptions were searched and which were not."""
    lines = ["No Microsoft Foundry resource was found."]
    describe = getattr(report, "describe", None)
    searched = tuple(getattr(report, "searched", ()) or ())
    unreachable = dict(getattr(report, "unreachable", {}) or {})
    if callable(describe) and searched:
        lines += ["", f"Searched {len(searched)} subscription(s):", describe()]
    if unreachable:
        lines += [
            "",
            f"{len(unreachable)} of them could NOT be searched, so the resource may well "
            "exist in one of those.",
        ]
    lines += [
        "",
        "Name the resource directly, or search a single subscription:",
        "  foundry configure --endpoint <resource-or-url>",
        "  foundry configure --subscription <subscription-id>",
    ]
    lines.append(
        "  (az account list --output table lists what you can see)"
        if not subscription
        else "  (or create a resource at https://ai.azure.com)"
    )
    return "\n".join(lines)


def _scan_accounts(subscription: str | None) -> tuple[list[discovery.Account], Any]:
    """List visible accounts plus, when discovery offers it, the scan report."""
    scan = getattr(discovery, "scan_accounts", None)
    with console.spinner("Looking for Microsoft Foundry resources"):
        if callable(scan):
            accounts, report = scan(subscription)
            return list(accounts), report
        return list(discovery.list_accounts(subscription)), None


def _parse_agent_list(raw: str | None) -> list[str] | None:
    """Parse ``--agents``. ``None`` means "the user did not say"."""
    if raw is None:
        return None
    names = [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]
    if "all" in names:
        return list(AGENT_ORDER)
    if "none" in names:
        return []
    unknown = [n for n in names if n not in AGENT_ORDER]
    if unknown:
        raise UserError(
            f"Unknown agent(s): {', '.join(unknown)}.\n"
            f"  Known agents: {', '.join(AGENT_ORDER)} (or 'all')."
        )
    return [tool for tool in AGENT_ORDER if tool in names]


def _select_agents(requested: list[str] | None) -> list[Agent]:
    """Turn the request into agent objects, prompting when nothing was asked for."""
    if requested is not None:
        return [_load_agent(tool) for tool in requested]

    available = _known_agents()
    installed = [a for a in available if _is_installed(a)]

    if not _interactive():
        if not installed:
            console.warn("No agent CLI was found on PATH; configuring the endpoint only.")
            for agent in available:
                console.note(f"{_display(agent)}: {_install_hint(agent)}")
        return installed

    labels = {
        agent.name: _display(agent) + ("" if _is_installed(agent) else "  (not installed)")
        for agent in available
    }
    picked = _ask_checkbox(
        "Which agents should foundry configure?",
        [labels[a.name] for a in available],
        [labels[a.name] for a in installed],
    )
    chosen = [a for a in available if labels[a.name] in picked]
    if not chosen:
        console.warn("No agent selected; configuring the endpoint only.")
    return chosen


def _known_agents() -> list[Agent]:
    """Every agent module this installation actually has."""
    found: list[Agent] = []
    for tool in AGENT_ORDER:
        try:
            found.append(_load_agent(tool))
        except UserError as exc:
            console.warn(str(exc).splitlines()[0])
    return found


# --------------------------------------------------------------------------- #
# Profile construction                                                          #
# --------------------------------------------------------------------------- #


def _build_profile(endpoint: str | None, subscription: str | None) -> Profile:
    """Discover the resource and its deployments, and return a fresh profile.

    Reachability is proved before the ARM scan because it is one request and it
    produces the precise message for a 401/403/404 (SPEC section 9), whereas the
    scan is slow and would report a mistyped host as "not found in 5
    subscriptions" -- sending the user after the wrong problem.
    """
    root = _normalize(endpoint) if endpoint else _resolve_endpoint(None, subscription)

    with console.spinner(f"Checking {root}"):
        discovery.reachable(root)

    with console.spinner("Locating the resource in Azure Resource Manager"):
        account = discovery.find_account(root, subscription)
    if account is None:
        raise UserError(
            f"No Foundry resource behind {root} was found in the subscriptions searched.\n"
            "  Search a specific one:\n"
            f"    foundry configure --endpoint {root} --subscription <subscription-id>\n"
            "  (az account list --output table lists what you can see)"
        )

    with console.spinner(f"Listing deployments on {account.name}"):
        published = discovery.deployments(account)

    anthropic: dict[str, str] = {}
    openai: list[str] = []
    for deployment in published:  # discovery.deployments returns newest-first
        family = discovery.family(deployment)
        if family in ("opus", "sonnet", "haiku"):
            anthropic.setdefault(family, deployment.name)
        elif family == "openai":
            openai.append(deployment.name)

    if not anthropic and not openai:
        raise UserError(
            f"{account.name} has no chat deployment a coding agent can use.\n"
            f"  It publishes {len(published)} deployment(s), none of them an Anthropic or "
            "OpenAI chat model.\n"
            "  Publish one in the Microsoft Foundry portal (https://ai.azure.com), then re-run:\n"
            "    foundry configure"
        )

    return profile.Profile(
        endpoint=account.endpoint,
        subscription=account.subscription,
        resource_group=account.resource_group,
        account=account.name,
        deployments={"anthropic": anthropic, "openai": openai},
        agents={},
    )


def _require_signin() -> None:
    """Refuse to configure without a usable Azure session (SPEC section 9).

    A sign-in is offered rather than performed: ``az login`` opens a browser, and
    a command that does that unasked inside a script is worse than one that fails
    naming the exact command to run.
    """
    if auth.signed_in():
        return
    if _interactive() and _confirm("Not signed in to Azure. Sign in now?", default=True):
        auth.login()
        if auth.signed_in():
            return
    raise UserError("Not signed in to Azure.\n  Run: az login")


# --------------------------------------------------------------------------- #
# Agent loading                                                                 #
# --------------------------------------------------------------------------- #

_AGENT_CACHE: dict[str, Agent] = {}


def _load_agent(tool: str) -> Agent:
    """Return the ``Agent`` implementation for *tool*.

    Resolution is deliberately tolerant. SPEC section 10 fixes the *protocol*
    each agent module satisfies but not the name of the symbol carrying it, so
    this accepts a registry in ``agents.base``, an ``AGENT`` constant, a factory,
    or a single suitable class or instance in the module -- rather than making
    the launcher depend on a naming convention nobody wrote down.
    """
    cached = _AGENT_CACHE.get(tool)
    if cached is not None:
        return cached
    agent = _from_base_registry(tool) or _from_agent_module(tool)
    _AGENT_CACHE[tool] = agent
    return agent


def _from_base_registry(tool: str) -> Agent | None:
    """Use ``agents.base``'s own registry, when it has one."""
    try:
        base = importlib.import_module("foundry.agents.base")
    except ImportError:
        return None

    mapping = getattr(base, "AGENTS", None)
    if isinstance(mapping, dict):
        candidate = _instantiate(mapping.get(tool))
        if _is_agent(candidate):
            return candidate

    getter = getattr(base, "get", None)
    if callable(getter):
        try:
            candidate = _instantiate(getter(tool))
        except Exception:
            return None
        if _is_agent(candidate):
            return candidate
    return None


def _from_agent_module(tool: str) -> Agent:
    try:
        module = importlib.import_module(f"foundry.agents.{tool}")
    except ImportError as exc:
        raise UserError(
            f"foundry has no module for '{tool}' (foundry.agents.{tool} is missing).\n"
            "  This installation is incomplete; reinstall foundry."
        ) from exc

    for attribute in ("AGENT", "agent", "get_agent", "build"):
        candidate = _instantiate(getattr(module, attribute, None))
        if _is_agent(candidate):
            return candidate

    # Nothing named by convention: take the one suitable object in the module.
    for value in vars(module).values():
        if not isinstance(value, type) and _is_agent(value) and value.name == tool:
            return value
    for value in vars(module).values():
        if isinstance(value, type) and getattr(value, "__module__", "") == module.__name__:
            candidate = _instantiate(value)
            if _is_agent(candidate):
                return candidate

    raise UserError(
        f"foundry.agents.{tool} exposes no agent object.\n"
        "  This installation is inconsistent; reinstall foundry."
    )


def _instantiate(value: Any) -> Any:
    """Call *value* when it is a class or a zero-argument factory."""
    if value is None or _is_agent(value):
        return value
    if isinstance(value, type) or callable(value):
        try:
            return value()
        except Exception:
            return None
    return None


def _is_agent(value: Any) -> bool:
    return value is not None and all(hasattr(value, attr) for attr in _AGENT_ATTRS)


def _is_installed(agent: Agent) -> bool:
    try:
        return bool(agent.is_installed())
    except Exception:
        return shutil.which(str(getattr(agent, "binary", "") or "")) is not None


def _display(agent: Agent) -> str:
    return str(getattr(agent, "display", None) or getattr(agent, "name", "the agent"))


def _install_hint(agent: Agent) -> str:
    """How to install this agent, preferring what the agent module itself says."""
    own = getattr(agent, "install_hint", None)
    if isinstance(own, str) and own.strip():
        return own.strip()
    command = _INSTALL_HINTS.get(str(getattr(agent, "name", "")))
    if command:
        return f"Install it with: {command}"
    return f"Install {_display(agent)} and make sure `{agent.binary}` is on PATH."


def _missing_binary_message(agent: Agent) -> str:
    return (
        f"{_display(agent)} is not installed: `{agent.binary}` is not on PATH.\n"
        f"  {_install_hint(agent)}"
    )


# --------------------------------------------------------------------------- #
# Prompting                                                                     #
# --------------------------------------------------------------------------- #


def _interactive() -> bool:
    """True only when there is a human on both ends of the pipe."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _questionary() -> Any:
    """Import questionary lazily; it pulls in prompt_toolkit, which is not cheap."""
    import questionary

    return questionary


def _answer(question: Any) -> Any:
    """Run one prompt, treating Ctrl+C as an abort rather than a crash."""
    try:
        value = question.ask()
    except KeyboardInterrupt as exc:
        raise typer.Abort() from exc
    if value is None:
        raise typer.Abort()
    return value


def _confirm(message: str, *, default: bool) -> bool:
    return bool(_answer(_questionary().confirm(message, default=default)))


def _ask_select(message: str, choices: list[str]) -> str:
    return str(_answer(_questionary().select(message, choices=choices)))


def _ask_checkbox(message: str, choices: list[str], checked: list[str]) -> list[str]:
    q = _questionary()
    options = [q.Choice(title=choice, checked=choice in checked) for choice in choices]
    return list(_answer(q.checkbox(message, choices=options)))


def _ask_endpoint() -> str:
    prompt = _questionary().text(
        "Foundry resource name or endpoint URL:",
        validate=lambda v: bool(v.strip()) or "Enter a resource name or URL",
    )
    return str(_answer(prompt)).strip()


# --------------------------------------------------------------------------- #
# Small helpers                                                                 #
# --------------------------------------------------------------------------- #


def _normalize(value: str) -> str:
    """``endpoints.normalize`` with its ``ValueError`` promoted to a user error."""
    try:
        return endpoints.normalize(value)
    except ValueError as exc:
        raise UserError(str(exc)) from exc


def _short(path: Path | str) -> str:
    """Render a path with ``~`` for the home directory, for readable output."""
    text = str(path)
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):  # pragma: no cover - no home directory
        return text
    if home and text.lower().startswith(home.lower()):
        return "~" + text[len(home) :]
    return text


# --------------------------------------------------------------------------- #
# Entry point                                                                   #
# --------------------------------------------------------------------------- #


def main() -> None:
    """Console-script entry point. Maps every failure onto SPEC section 7's codes."""
    try:
        app()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        console.warn("Interrupted.")
        raise SystemExit(EXIT_INTERRUPTED) from None
    except (UserError, auth.AuthError, profile.ProfileError) as exc:
        _report(exc, exit_code=EXIT_USER)
    except Exception as exc:  # a foundry bug: exit 2, and say so plainly
        _report(exc, exit_code=EXIT_INTERNAL, internal=True)


def _report(exc: BaseException, *, exit_code: int, internal: bool = False) -> NoReturn:
    """Print one actionable failure -- never a traceback -- and exit.

    ``FOUNDRY_DEBUG`` re-raises instead, because the one thing worse than a
    traceback in front of a user is no traceback in front of a maintainer.
    """
    if os.environ.get(DEBUG_ENV):
        raise exc
    message = str(exc) or exc.__class__.__name__
    if internal:
        console.fail(f"Internal error: {exc.__class__.__name__}: {message}")
        console.note(f"This is a foundry bug. Re-run with {DEBUG_ENV}=1 for the traceback.")
    else:
        first, _, rest = message.partition("\n")
        console.fail(first)
        for line in rest.splitlines():
            # `note` indents by two, so drop one level of the message's own
            # indentation rather than all of it: the remedy lines in these
            # messages are often a command indented under its explanation.
            console.note(line.removeprefix("  ").rstrip())
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
