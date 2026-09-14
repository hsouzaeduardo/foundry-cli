# Agent CLI configuration reference

Normative for `docs/SPEC.md` §6. Compiled from each vendor's public documentation
and from direct observation of the installed CLIs. Nothing here comes from
another implementation.

Throughout, `E` is the normalised Foundry resource root:
`https://<resource>.services.ai.azure.com`.

**The single most important column is what each client appends to the base URL.**
Get that wrong and nothing works.

| Agent | Base URL to configure | Client appends | Resulting request |
|---|---|---|---|
| Claude Code | *(native mode — no base URL)* | — | `E/anthropic/v1/messages` |
| Codex | `E/openai/v1` | `/responses` | `E/openai/v1/responses` |
| Copilot | `E` | `/openai/v1/chat/completions` | `E/openai/v1/chat/completions` |
| OpenCode (anthropic) | `E/anthropic/v1` | `/messages` | `E/anthropic/v1/messages` |
| OpenCode (openai) | `E/openai/v1` | `/responses` | `E/openai/v1/responses` |
| Pi (anthropic) | `E/anthropic` | `/v1/messages` | `E/anthropic/v1/messages` |
| Pi (openai) | `E/openai/v1` | `/responses` | `E/openai/v1/responses` |

Note that OpenCode's Anthropic provider appends only `/messages` while Pi's
appends `/v1/messages` — the `/v1` segment therefore belongs in OpenCode's base
URL and not in Pi's. This asymmetry is the easiest thing to get wrong.

---

## 1. Claude Code — native Microsoft Foundry mode

Claude Code supports Microsoft Foundry as a first-class provider. **Use that
rather than a custom base URL.** It is vendor-supported, needs no credential
helper, and `/status` reports `Microsoft Foundry`.

Isolation variable: **`CLAUDE_CONFIG_DIR`** relocates the whole `.claude` config
directory.

```
CLAUDE_CODE_USE_FOUNDRY=1
ANTHROPIC_FOUNDRY_RESOURCE=<resource name>     # or ANTHROPIC_FOUNDRY_BASE_URL=E/anthropic
ANTHROPIC_DEFAULT_OPUS_MODEL=<deployment>
ANTHROPIC_DEFAULT_SONNET_MODEL=<deployment>
ANTHROPIC_DEFAULT_HAIKU_MODEL=<deployment>
ENABLE_PROMPT_CACHING_1H=1                     # optional; 1-hour cache TTL
```

**Pinning the three model variables is mandatory, not cosmetic.** Anthropic's own
documentation warns that without them the `opus`/`sonnet` aliases resolve to
Claude Code's built-in Foundry defaults, which lag current releases and may not
exist in the account — and Foundry performs no startup model check, so the
request simply fails. Discovering the real deployment names and pinning them is
the main thing `foundry` contributes here.

### Authentication — three options, in the vendor's precedence order

1. `ANTHROPIC_FOUNDRY_AUTH_TOKEN` — a bearer token, sent as
   `Authorization: Bearer`. Highest precedence. Requires Claude Code ≥ 2.1.203.
2. `ANTHROPIC_FOUNDRY_API_KEY` — the resource API key.
3. Neither set → the Azure SDK `DefaultAzureCredential` chain, which picks up
   `az login` and **refreshes on its own**.

**Prefer option 3.** A token from option 1 is static: Foundry tokens live 72–90
minutes and Claude Code will not renew one, so a long session dies mid-flight.

**But option 3 has a trap, observed in practice.** `DefaultAzureCredential` tries
`EnvironmentCredential` *first*. If `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` /
`AZURE_TENANT_ID` are present in the environment, it uses that service principal —
and if the secret is expired or the SP belongs to a different tenant than the
active `az login`, authentication fails outright rather than falling through:

```
API Error: Failed to get token from azureADTokenProvider:
EnvironmentCredential authentication failed ... AADSTS7000222:
The provided client secret keys for app '...' are expired.
```

**Therefore: before launching Claude Code, remove `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` from the child environment whenever
they do not correspond to the active `az` account.** With those cleared, the
chain reaches `AzureCliCredential` and works with automatic refresh. Both the
failure and the fix were reproduced end to end.

Fall back to option 1 (mint with `az` and export
`ANTHROPIC_FOUNDRY_AUTH_TOKEN`) only where the credential chain cannot be made to
work; warn the user that the session is then time-limited.

Claude Code sends the `anthropic-version: 2023-06-01` header itself — do not add it.

Other useful facts: `--settings <file-or-json>` loads a settings file for the
session (settings-file `env` values beat shell environment variables);
`--setting-sources user,project,local` controls which files load; `--model`
overrides both the `model` setting and `ANTHROPIC_MODEL`.

---

## 2. Codex

Isolation variable: **`CODEX_HOME`** — verified. Setting it makes Codex read
`$CODEX_HOME/config.toml` and ignore `~/.codex/config.toml` entirely. Confirmed
on a machine whose real `~/.codex/config.toml` fails to parse; with `CODEX_HOME`
pointed elsewhere, Codex started normally.

Write `$CODEX_HOME/config.toml`:

```toml
model = "<deployment name>"
model_provider = "foundry"

[model_providers.foundry]
name = "Microsoft Foundry"
base_url = "E/openai/v1"
wire_api = "responses"

[model_providers.foundry.auth]
command = "<absolute path to the foundry executable>"
args = ["auth-token", "--endpoint", "E", "--subscription", "<sub>"]
timeout_ms = 5000
refresh_interval_ms = 900000
```

- **`wire_api` must be `"responses"`.** `"chat"` is a hard deserialization error
  in current Codex: *"`wire_api = \"chat\"` is no longer supported."*
- **Provider ids `openai`, `ollama` and `lmstudio` are reserved** and rejected.
  `foundry` is fine.
- `name` must be non-empty or validation fails.
- URL joining is `base_url.trim_end_matches('/') + "/" + path`, so
  `E/openai/v1` + `/responses` is correct.
- The `[.auth]` table is the command-backed bearer mechanism: Codex re-runs
  `command` every `refresh_interval_ms`, so sessions survive token expiry. Prefer
  it over `env_key` (static) and over `experimental_bearer_token` (discouraged by
  the vendor).
- `query_params` exists for Azure's classic `api-version` route. Not needed:
  `/openai/v1` is versionless.
- `-c key=value` overrides any config value on the command line and accepts dotted
  paths — useful for one-off launches, but it does not bypass parsing of the
  config file, so it cannot rescue a broken one. `CODEX_HOME` can.

---

## 3. GitHub Copilot CLI

Isolation variable: **`COPILOT_HOME`** — replaces the whole `~/.copilot`
directory. Provider configuration is **environment-only**; there is no
`settings.json` key for base URL, provider type or credential.

```
COPILOT_PROVIDER_TYPE=azure
COPILOT_PROVIDER_BASE_URL=E
COPILOT_PROVIDER_API_KEY=<resource api key>
COPILOT_PROVIDER_MODEL_ID=<well-known model id>     # e.g. gpt-5.4
COPILOT_PROVIDER_WIRE_MODEL=<deployment name>
COPILOT_OFFLINE=true                                 # optional
```

- `COPILOT_PROVIDER_TYPE` selects both wire protocol and auth header:
  `openai` → `Authorization: Bearer`; `azure` → `api-key:`;
  `anthropic` → `x-api-key:`.
- With type `azure` and `COPILOT_PROVIDER_AZURE_API_VERSION` **unset**, the
  request is the versionless `POST <base>/openai/v1/chat/completions` — so
  `COPILOT_PROVIDER_BASE_URL` is the bare resource root `E`, not `E/openai/v1`.
  Setting the api-version variable switches to the classic deployment route;
  leave it unset.
- `COPILOT_PROVIDER_WIRE_API=responses` switches to `<base>/responses`. Vendor
  guidance is to use it for GPT-5-series models; validate before adopting.
- **`COPILOT_PROVIDER_MODEL_ID` / `COPILOT_PROVIDER_WIRE_MODEL` split the model
  identity** — the first is a well-known id used for token limits and agent
  config, the second is what goes on the wire. This exists precisely for Azure
  deployment names, and is the correct way to express them. `COPILOT_MODEL` sets
  both at once.
- Type `azure` authenticates with a **key**, not a bearer. Where only Entra ID is
  available, use `COPILOT_PROVIDER_TYPE=openai` with
  `COPILOT_PROVIDER_BEARER_TOKEN` and `COPILOT_PROVIDER_BASE_URL=E/openai/v1`.
  `COPILOT_PROVIDER_BEARER_TOKEN` outranks `COPILOT_PROVIDER_API_KEY`.
- Copilot BYOK has **no credential-refresh mechanism** — the value is static.
  Refresh it on a timer for the life of the child process.
- `COPILOT_PROVIDER_HEADERS` takes newline-separated `Name: Value` pairs.
- `COPILOT_PROVIDER_MAX_PROMPT_TOKENS` / `_MAX_OUTPUT_TOKENS` override token
  limits for models absent from Copilot's catalogue.

---

## 4. OpenCode

Isolation variables: **`OPENCODE_CONFIG`** (path to a single config file) or
**`OPENCODE_CONFIG_DIR`** (a config directory). Note the global config dir is
`XDG_CONFIG_HOME || ~/.config` + `/opencode` **on every platform including
Windows** — the `xdg-basedir` package has no per-OS branching.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "foundry-anthropic/<deployment>",
  "provider": {
    "foundry-anthropic": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "E/anthropic/v1", "apiKey": "<token>" },
      "models": { "<deployment>": {} }
    },
    "foundry-openai": {
      "npm": "@ai-sdk/openai",
      "options": { "baseURL": "E/openai/v1", "apiKey": "<token>" },
      "models": { "<deployment>": {} }
    }
  }
}
```

- `@ai-sdk/anthropic` posts to **`{baseURL}/messages`** — hence `/anthropic/v1`
  in the base URL.
- `@ai-sdk/openai` posts to **`{baseURL}/responses`** by default.
- `@ai-sdk/openai-compatible` posts to `{baseURL}/chat/completions`.
- `models` is an **object keyed by model id**, not an array. The key is what is
  sent unless an entry overrides it with `id`.
- Top-level `model` is the string `"<providerID>/<modelID>"`.
- `apiKey` supports `{env:VAR}` interpolation, which is preferable to embedding a
  token in the file.
- ~25 AI-SDK packages are bundled; `@ai-sdk/anthropic` and `@ai-sdk/openai` need
  no install.
- The credential is static; refresh it while the child runs.

---

## 5. Pi

Isolation variable: **`PI_CODING_AGENT_DIR`**.

```json
{
  "providers": {
    "foundry-claude": {
      "baseUrl": "E/anthropic",
      "api": "anthropic-messages",
      "apiKey": "<token>",
      "authHeader": true,
      "models": [{ "id": "<deployment>" }]
    },
    "foundry-openai": {
      "baseUrl": "E/openai/v1",
      "api": "openai-responses",
      "apiKey": "<token>",
      "models": [{ "id": "<deployment>" }]
    }
  }
}
```

Path appended per dialect:

| `api` | appends |
|---|---|
| `anthropic-messages` | `/v1/messages` |
| `openai-responses` | `/responses` |
| `openai-completions` | `/chat/completions` |

So the `/v1` belongs in the base URL for the OpenAI dialects and **not** for
`anthropic-messages`. `settings.json` in the same directory carries
`defaultProvider` and `defaultModel`. The credential is static; refresh it.

---

## 6. Summary of isolation variables

Every supported agent can be pointed at a private configuration root, so
`foundry` never needs to write into a file the user owns:

| Agent | Variable |
|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR` |
| Codex | `CODEX_HOME` |
| Copilot | `COPILOT_HOME` |
| OpenCode | `OPENCODE_CONFIG_DIR` (or `OPENCODE_CONFIG`) |
| Pi | `PI_CODING_AGENT_DIR` |

Use them. `foundry revert` then reduces to deleting `~/.foundry/`, and a user's
existing setup is untouched and unreadable by us.

---

## 7. Credential refresh, by agent

| Agent | Mechanism | Refreshes itself? |
|---|---|---|
| Claude Code | `DefaultAzureCredential` (native Foundry mode) | **yes** |
| Codex | `[model_providers.foundry.auth]` command | **yes**, every `refresh_interval_ms` |
| Copilot | static env var | no — refresh on a timer |
| OpenCode | static `apiKey` in config | no — refresh on a timer |
| Pi | static `apiKey` in config | no — refresh on a timer |

For the three static cases, rewrite the credential every 30 minutes while the
child process lives. Measured Foundry token lifetime is 72–90 minutes.
