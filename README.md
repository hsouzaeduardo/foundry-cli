# foundry

Run your coding agents against [Microsoft Foundry](https://ai.azure.com/).

```bash
az login
foundry claude
```

`foundry` points **Claude Code, Codex, GitHub Copilot CLI, OpenCode and Pi** at a
Microsoft Foundry endpoint. It finds the models you actually have deployed, writes
each agent's configuration, and starts the agent. Your Azure identity is the only
credential — there are no API keys to distribute.

---

## Why

Every coding-agent CLI has its own config file, its own credential scheme and its
own idea of where an endpoint lives. Wiring five of them to the same Azure
resource by hand means five different sets of environment variables, and every
developer repeating the exercise.

`foundry` does it once:

- **No API keys.** Authentication is your `az login`, through Microsoft Entra ID.
  Access is governed by Azure RBAC on the resource; when someone leaves, their
  access leaves with them.
- **Models come from your resource.** It reads your Foundry deployments from
  Azure Resource Manager, so the model list is whatever your team actually
  published — not a hard-coded guess.
- **It never touches your existing setup.** Every agent is pointed at its own
  configuration directory under `~/.foundry/`. Your `~/.claude`, `~/.codex` and
  `~/.copilot` are neither read nor written.

## Requirements

- Python 3.11+
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`)
- A Microsoft Foundry resource with at least one chat model deployed, and the
  `Azure AI User` or `Cognitive Services User` role on it
- The agent CLIs you want to launch, installed

## Install

```bash
uv tool install foundry-cli
```

## Use

```bash
az login
foundry configure          # pick an endpoint, pick your agents
foundry claude             # or codex, copilot, opencode, pi
```

Arguments pass straight through to the underlying CLI:

```bash
foundry claude -r
foundry codex --full-auto
foundry claude --model claude-opus-4-7
```

Other commands:

```bash
foundry status             # what is configured, and against which Azure resource
foundry revert             # remove everything foundry created
```

## Choosing a model

Foundry deployment names are chosen by whoever published them, and are not
necessarily the model's name. `foundry` discovers them and uses them verbatim —
list yours with:

```bash
az cognitiveservices account deployment list -n <resource> -g <resource-group> -o table
```

The `deployment` column is what `--model` expects.

## How it authenticates

`foundry` shells out to the Azure CLI for a short-lived Entra ID token; it stores
no secret of its own. Where an agent supports a credential *command* (Claude Code,
Codex) it is given one, so long sessions survive token expiry. Where an agent only
accepts a static value, `foundry` refreshes it while the agent runs.

Two escape hatches exist for environments where `az login` is impractical:
`FOUNDRY_BEARER` (a pre-minted token) and `FOUNDRY_API_KEY` (a resource key).
Model discovery still needs `az`, because Azure Resource Manager has no key-based
authentication.

## What it does not do

By design, this is a launcher and nothing more. It has no managed configuration,
no MCP registration, no usage reporting, no telemetry, and no model routing.

Gemini is not supported: Microsoft Foundry serves no Google models.

## Contributing

Issues and pull requests are welcome. Adding support for another agent CLI means
adding one module under `src/foundry/agents/` and one line in the registry — see
`docs/SPEC.md` for the architecture and `docs/agent-config-reference.md` for how
each agent is configured.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
