# `foundry` — functional specification

Version 1. This document defines the product. It is written from two sources only:

1. `docs/foundry-api-notes.md` — Microsoft Foundry API behaviour measured directly
   against a live Azure subscription.
2. The public vendor documentation of each coding-agent CLI (see
   `docs/agent-config-reference.md`).

**Implementation constraint.** Whoever implements this must work from this
specification and the two reference documents above. Do not consult any other
codebase.

---

## 1. What it is

`foundry` is a launcher. It points existing coding-agent CLIs at a Microsoft
Foundry endpoint and starts them.

```
$ az login
$ foundry claude
```

That is the whole product. The user already has the agent CLIs installed and
already has an Azure identity; `foundry` removes the step where they hand-edit
five different config files with five different credential schemes.

### Explicit non-goals for v1

Not in scope, and not to be designed "for later":

- MCP server registration
- Admin-published / managed configuration
- Skills distribution
- Tracing or telemetry export
- Usage reporting, budgets, spend routing
- Model routing or fallback logic
- Any local proxy or relay process
- Gemini (Foundry serves no Google models)
- Cursor (it runs models on Cursor's own account; with MCP out of scope there is
  nothing for `foundry` to configure)

Supported agents: **Claude Code, Codex, GitHub Copilot CLI, OpenCode, Pi.**

---

## 2. Core concepts

### Endpoint

A Microsoft Foundry resource endpoint:

```
https://<resource>.services.ai.azure.com
```

The user may supply the bare resource name (`my-resource`), the host, or a full
URL with or without a scheme or trailing slash. All normalise to the form above.
A project endpoint (`.../api/projects/<project>`) is accepted as input, but every
inference URL is built from the **resource root** — the project path segment does
not serve inference.

### Deployment

A model published on that resource. **The deployment name is chosen freely by
whoever published it and is what goes in the request `model` field.** It is not
necessarily the model's name. `foundry` never parses a deployment name to infer
anything; it reads the model family from Azure Resource Manager metadata.

### Subscription

The Azure subscription containing the resource. Resolved automatically; the user
only supplies it when the automatic scan is ambiguous or too slow.

---

## 3. Authentication

`foundry` shells out to the Azure CLI. It embeds no credential library, stores no
secret, and never asks the user for a key.

| Purpose | Command |
|---|---|
| Inference token | `az account get-access-token --resource https://ai.azure.com --output json` |
| Management token | `az account get-access-token --resource https://management.azure.com --output json` |
| Identity | `az account show --output json` |
| Subscriptions | `az account list --output json` |

Read `accessToken` and `expiresOn` from the JSON. Cache each token in-process,
keyed by `(resource, subscription)`, and re-mint at 80% of its remaining lifetime.
Measured token lifetimes are 72–90 minutes, so a 30-minute refresh cadence is
comfortably safe.

If a mint against `https://ai.azure.com` fails, retry once against
`https://cognitiveservices.azure.com` — both are accepted by the data plane, and
some tenants issue only one of them.

**Windows: `az` is a `.cmd` shim.** Every subprocess call must set `shell=True` on
win32, and must pass `encoding="utf-8", errors="replace"`. Without the explicit
encoding, `az` output containing non-ASCII (a tenant display name, for instance)
raises `UnicodeDecodeError` inside subprocess's reader thread; the exception is
swallowed and `stdout` silently arrives as `None`. This is not theoretical — it
was observed.

**Never run `az login` when a usable session already exists.** Check first
(`az account show` succeeds *and* a token mint succeeds); only sign in when there
is nothing usable. Re-running `az login` over a working session is friction at
best, and fails outright wherever the profile directory is redirected.

### Escape hatches

- `FOUNDRY_BEARER` — a pre-minted inference token. Short-circuits inference-token
  minting only; management-plane calls still go through `az`, because an
  inference token is rejected by ARM.
- `FOUNDRY_API_KEY` — a resource API key used as the inference bearer instead of
  an Entra token. For headless environments where `az login` is impractical.
  Model discovery still requires `az` (ARM has no key-based auth).

---

## 4. Model discovery

The data-plane route `GET {endpoint}/openai/v1/models` returns the **regional
catalogue** — roughly 400 entries of what *could* be deployed. It is not the list
of this resource's deployments and must never be offered as one. Use it only to
prove the endpoint is reachable and authorised.

Deployments come from ARM:

```
GET https://management.azure.com/subscriptions/{sub}/providers
    /Microsoft.CognitiveServices/accounts?api-version=2025-06-01

GET https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers
    /Microsoft.CognitiveServices/accounts/{account}/deployments?api-version=2025-06-01
```

Resolving the resource requires subscription + resource group + account name,
which the endpoint host alone does not give. Scan the subscriptions from
`az account list`, match on the account's `properties.customSubDomainName` or the
host of `properties.endpoint`, and persist the result so later runs skip the scan.
Follow `nextLink` when present.

Per deployment, read:

| Field | Meaning |
|---|---|
| `name` | the request `model` value |
| `properties.model.name` | underlying model |
| `properties.model.format` | `OpenAI`, `Anthropic`, `Cohere`, … |
| `properties.model.version` | |
| `properties.provisioningState` | keep only `Succeeded` |

### Family classification

Drive it from `properties.model.format` first, falling back to the model name only
when the format is unfamiliar:

- `Anthropic` → `opus` / `sonnet` / `haiku` from the model name
- `OpenAI` and the name looks like a chat model → `openai`
- anything else chat-capable → `other`

Exclude non-chat deployments by model name. Observed markers that must be
excluded: `embed`, `embedding`, `rerank`, `whisper`, `transcribe`, `audio`,
`realtime`, `tts`, `image`, `dall-e`, `sora`, `flux`, `diffusion`, `moderation`,
`ocr`. Live listings offer speech-to-text and image-generation deployments
alongside chat ones; without this filter they are offered as chat models.

### Ordering

Sort newest-first. Version segments appear in **both** dotted and dashed forms —
`gpt-5.4-nano` and `claude-opus-4-7` — and a comparator that only understands
dashes ranks `gpt-4.1` above `gpt-5.4`. Parse both.

---

## 5. Endpoint routes

Foundry exposes each vendor's native API. No translation layer is needed, and
**no route takes an `api-version` query parameter.**

| Route | Dialect |
|---|---|
| `{endpoint}/anthropic` | Anthropic Messages — client appends `/v1/messages` |
| `{endpoint}/openai/v1` | OpenAI — client appends `/responses` or `/chat/completions` |

Both verified live, including Anthropic SSE streaming.

The Anthropic route **requires** an `anthropic-version` request header; without
it, it returns a native Anthropic `invalid_request_error`. Claude Code sends this
header itself, so `foundry` does not need to inject it — but anything that
proxies the route must forward it.

---

## 6. Agent configuration

For each agent, `foundry` writes a config file **that it owns**, and launches the
agent pointed at that file. It does not edit the user's existing config where the
agent offers any way to avoid it.

The exact key names, file locations and base-URL path-append behaviour for each
agent are specified in `docs/agent-config-reference.md`, which is derived from the
vendors' public documentation. That document is normative for those details;
this section states only the policy.

**Policy:**

1. **Prefer giving the agent its own home directory over writing into the user's.**
   Several agents accept an environment variable that relocates their entire
   config root; when one does, point it at a directory under `~/.foundry/` and
   write there. The user's own config is then never read, never written, and
   never at risk — and `revert` is a directory delete.

   This is verified for Codex: `CODEX_HOME=<dir>` makes it read `<dir>/config.toml`
   and ignore `~/.codex/config.toml` entirely. It was confirmed on a machine whose
   real `~/.codex/config.toml` **fails to parse** on the installed Codex version;
   with `CODEX_HOME` set, Codex started normally. Isolation therefore also buys
   immunity from a user config that is already broken.

   Where no such variable exists, use a `foundry`-owned config file plus a launch
   flag (Claude Code: `--settings <file>`). Only where neither is possible may a
   shared file be edited — and then back it up to `~/.foundry/backups/` first and
   touch only the keys `foundry` owns.

   Investigate the relocation variable for every agent before falling back to a
   shared-file write. Do not assume one does not exist because the `--help` text
   omits it.
2. Prefer a credential *command* over a baked-in token wherever the agent
   supports one, so a long session cannot die on an expired token. Where only a
   static value is supported, refresh it on a timer while the agent runs.
3. Record which keys were written, so `foundry revert` can remove exactly those
   and nothing else.
4. Set the model by deployment name, verbatim. Never rewrite, suffix, or
   canonicalise it.

---

## 7. Command surface

```
foundry claude | codex | copilot | opencode | pi   launch an agent
foundry configure                                  set up the endpoint and agents
foundry status                                     what is configured, and against what
foundry revert                                     undo every change foundry made
foundry --version
```

### Launch

`foundry <agent> [args...]` configures the agent if it is not configured yet,
then execs the agent's CLI. Every unrecognised argument is passed through
untouched — `foundry claude -r` must reach Claude Code as `-r`.

Options:

- `--model <deployment>` — launch with a specific deployment
- `--endpoint <url>` — use (and if needed set up) a different endpoint

### `foundry configure`

Options: `--endpoint <url>`, `--subscription <id>`, `--agents a,b`,
`--dry-run` (print what would be written, write nothing).

With no options it prompts: which endpoint, then which agents. If exactly one
Foundry resource is visible, offer it as the default rather than asking the user
to type a URL.

### `foundry auth-token` (hidden)

Prints a bearer token to stdout and exits. This is what the agents' credential
commands invoke; it is not for humans. Options: `--endpoint`, `--subscription`.

### Exit codes

`0` success · `1` user-fixable error (not signed in, no deployment, missing
binary) · `2` internal error.

---

## 8. State

One file: `~/.foundry/config.json`.

```json
{
  "version": 1,
  "endpoint": "https://my-resource.services.ai.azure.com",
  "subscription": "<uuid>",
  "resource_group": "rg-example",
  "account": "my-resource",
  "deployments": {
    "anthropic": {"opus": "claude-opus-4-7", "sonnet": "claude-sonnet-4-6"},
    "openai": ["gpt-5.4-mini", "gpt-5.4-nano"]
  },
  "agents": {
    "claude": {"configured_at": "…", "owns": ["…"]}
  }
}
```

`owns` records the keys or files `foundry` wrote, so `revert` is exact. Tokens are
never persisted. An unrecognised `version` is discarded and rebuilt rather than
migrated.

Backups live in `~/.foundry/backups/`.

---

## 9. Errors

Every failure names the concrete fix.

| Condition | Message must say |
|---|---|
| not signed in | run `az login` |
| 403 from the endpoint | grant `Azure AI User` on the resource |
| 404 from the endpoint | this is not a Foundry endpoint; check the host |
| no chat deployment | publish one in the Foundry portal |
| agent binary missing | how to install that agent |
| resource not found in any subscription | which subscriptions were searched, and to pass `--subscription` |

When a multi-subscription scan cannot obtain a token for some subscriptions, say
so. Reporting "no resource found" when the truth is "three subscriptions were
unreachable" sends the user hunting for the wrong problem.

---

## 10. Architecture

```
src/foundry/
  cli.py          command surface, argument parsing, passthrough
  auth.py         az invocation, token cache, the Windows subprocess rules
  discovery.py    ARM scan, deployment listing, family classification, ordering
  endpoints.py    endpoint normalisation and route construction
  profile.py      ~/.foundry/config.json read/write, backups, revert bookkeeping
  console.py      output helpers
  agents/
    base.py       the protocol every agent module satisfies
    claude.py  codex.py  copilot.py  opencode.py  pi.py
```

Each agent module exposes: `name`, `binary`, `is_installed()`,
`configure(profile, deployment)`, `launch(profile, args)`, `revert(profile)`.
Adding an agent means adding one module and registering it — nothing else.

Dependencies: `typer`, `rich`, `questionary`, `tomlkit` (Codex writes TOML),
and the standard library for HTTP. No Azure SDK — authentication is `az`, and
the two ARM calls are plain GETs.

Python 3.11+. Windows, macOS and Linux.

---

## 11. Testing

- Unit tests mock at the subprocess and HTTP boundary. No test may require a real
  `az` binary or network.
- Each agent module is tested for the exact bytes it writes and the exact argv it
  execs.
- Endpoint normalisation, family classification, the version comparator (dotted
  *and* dashed) and the non-chat exclusion list each get direct table-driven tests.
- One opt-in end-to-end test, gated on `FOUNDRY_TEST_ENDPOINT`, that configures an
  agent against a real resource and asserts the written config.

Assertions are exact. A test that would pass against a broken implementation is
not a test.

---

## 12. Licence

Apache-2.0. Every source file carries the SPDX header
`# SPDX-License-Identifier: Apache-2.0`.
