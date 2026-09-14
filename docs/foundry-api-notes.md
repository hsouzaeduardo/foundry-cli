# Microsoft Foundry — facts VERIFIED LIVE against a real subscription

All of the below was executed against a real Azure subscription on 2026-08-23.
Subscription, tenant and resource identifiers have been replaced with
placeholders; every URL shape, header, field name and status code is verbatim. These are not
documentation claims — they are observed responses. Treat them as ground truth
and prefer them over any web-research claim that disagrees.

## Auth

| Purpose | Command | Verified |
|---|---|---|
| Data-plane inference token | `az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv` | yes, 200 on all inference routes |
| Management-plane (ARM) token | `az account get-access-token --resource https://management.azure.com --query accessToken -o tsv` | yes, 200 on ARM routes |
| Current account | `az account show -o json` → `{id, tenantId, name, user.name, state}` | yes |

Header on every data-plane call: `Authorization: Bearer <token>`.
Content type `application/json`. Anthropic route also accepts/echoes
`anthropic-version: 2023-06-01`.

## Endpoint shapes

Account of `kind: AIServices` exposes (from
`az cognitiveservices account show --query properties.endpoints`):

- **AI Foundry API / Azure AI Model Inference API**: `https://<name>.services.ai.azure.com/`
- Azure OpenAI legacy: `https://<name>.openai.azure.com/`
- Cognitive Services: `https://<name>.cognitiveservices.azure.com/`

`<name>` is `properties.customSubDomainName`. **The `services.ai.azure.com`
host is the one to build every inference URL from.**

## Inference routes — VERIFIED 200

Base `B = https://<name>.services.ai.azure.com`

### 1. Anthropic native Messages API — THIS IS THE BIG ONE

```
POST {B}/anthropic/v1/messages
Authorization: Bearer <cognitiveservices token>
{"model":"claude-sonnet-4-6","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}
```
→ **HTTP 200**, native Anthropic response shape:
```json
{"model":"claude-sonnet-4-6","id":"msg_011Ce…","type":"message","role":"assistant",
 "content":[{"type":"text","text":"Hi"}],"stop_reason":"max_tokens",
 "usage":{"input_tokens":8,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,
          "cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":0},
          "output_tokens":1,"service_tier":"standard"}}
```
With an empty body it returns a native Anthropic-style error
(`{"error":{"code":"no_model_name",…}}`), i.e. the route genuinely exists.

**Consequence: `foundry claude` works natively.** Point Claude Code at
`ANTHROPIC_BASE_URL = {B}/anthropic` (Claude Code appends `/v1/messages`).
This is the direct analogue of the old Databricks `/ai-gateway/anthropic`.

### 2. OpenAI-compatible chat completions
```
POST {B}/openai/v1/chat/completions
{"model":"gpt-5.4-nano","max_completion_tokens":16,"messages":[…]}
```
→ **HTTP 200**, standard OpenAI shape (plus Azure `content_filter_results`
and `prompt_filter_results` extras). No `api-version` query param needed.

### 3. OpenAI Responses API (what Codex CLI uses)
```
POST {B}/openai/v1/responses
{"model":"gpt-5.4-nano","input":"hi","max_output_tokens":16}
```
→ **HTTP 200**, `{"id":"resp_…","object":"response","status":"completed", …}`.

So `OPENAI_BASE_URL = {B}/openai/v1` covers Codex (`/responses`) and every
OpenAI-compatible client (`/chat/completions`).

### 4. Foundry Models route (non-OpenAI, non-Anthropic families)
```
POST {B}/models/chat/completions?api-version=2024-05-01-preview
```
→ route exists (empty body gives `{"error":{"message":"Missed model deployment"}}`).

### 5. Routes that DO NOT exist (404)
- `GET {B}/openai/v1/deployments`
- `GET {B}/openai/deployments?api-version=2024-10-21`

## Model discovery

`GET {B}/openai/v1/models` → **HTTP 200 but it is the regional CATALOG, not the
account's deployments** — 407 entries on this resource. Entry shape:
```json
{"id":"claude-sonnet-4-5-20250929","object":"model","status":"succeeded",
 "lifecycle_status":"generally-available","created_at":1760918400,
 "deprecation":{"inference":1792368000},
 "capabilities":{"inference":true,"chat_completion":true,"embeddings":false,
                 "completion":false,"fine_tune":false}}
```
Useful for "what *could* be deployed" and for `capabilities.chat_completion`
filtering, **but the model id in an inference request must be a DEPLOYMENT
name**, not a catalog id.

**Deployments must therefore come from ARM:**
```
GET https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}
    /providers/Microsoft.CognitiveServices/accounts/{acct}/deployments
    ?api-version=2025-06-01          (2024-10-01 also returns 200)
```
Each item: `name` (= the deployment name used as `model`),
`properties.model.{name,format,version}`, `sku.{name,capacity}`.
`format` is the family discriminator: `OpenAI`, `Anthropic`,
`Black Forest Labs`, `Cohere`, `Mistral AI`, …

Accounts:
```
GET https://management.azure.com/subscriptions/{sub}/providers
    /Microsoft.CognitiveServices/accounts?api-version=2025-06-01
```
filter to `kind in (AIServices, OpenAI)`.

Projects under an account (both api-versions verified 200):
```
GET .../accounts/{acct}/projects?api-version=2025-06-01
```
→ `value[].{id,name:"<acct>/<project>",location,kind,properties.endpoints}`.
Project endpoint lives at `properties.endpoints["AI Foundry API"]`.

## A representative resource layout (identifiers replaced with placeholders)

Subscription and tenant identifiers below are placeholders; the shapes are real.

| Account | RG | Kind | Region | Notable deployments |
|---|---|---|---|---|
| `example-resource` | rg-example | AIServices | eastus2 | **claude-opus-4-7**, **claude-sonnet-4-6** (format Anthropic), gpt-5.4-nano, gpt-5.4-mini, gpt-4.1, text-embedding-3-small |
| `example-router-resource` | rg-example-dev | AIServices | eastus2 | **model-router**, gpt-5.6-terra/sol/luna, gpt-5.4-nano/mini, embeddings, realtime |
| `rg-example-resource-6896` | rg-example | AIServices | eastus | gpt-4.1, gpt-4.1-mini, text-embedding-3-large |
| `example-openai-resource` | rg-example-openai | OpenAI | eastus | gpt-4.1-mini |

`example-resource` has project `example-project`.

**`model-router` is a real deployable OpenAI model** — it is the natural Azure
analogue of the Databricks AI Gateway "smart routing" feature: deploy it and
send `model: "model-router"`, and Foundry picks the underlying model per request.

## Direct consequences for the port

| Old (Databricks AI Gateway) | New (Microsoft Foundry) |
|---|---|
| `{ws}/ai-gateway/anthropic` | `{B}/anthropic` → `/v1/messages` |
| `{ws}/ai-gateway/codex/v1` | `{B}/openai/v1` → `/responses` |
| `{ws}/ai-gateway/mlflow/v1` (OpenAI-compat) | `{B}/openai/v1` → `/chat/completions` |
| `{ws}/ai-gateway/gemini` | **no equivalent — Google models are not in Foundry** |
| serving-endpoint / model-service discovery | ARM `…/deployments?api-version=2025-06-01` |
| `databricks auth token` | `az account get-access-token --resource https://cognitiveservices.azure.com` |
| AI Gateway router | `model-router` deployment |

Gemini has no home here. Decide explicitly (drop the `gemini` command, or keep
it only for a user-supplied Google API key) — do not fake a Foundry Gemini URL.

## Addenda (second verification pass)

- **`anthropic-version: 2023-06-01` is a REQUIRED header** on
  `/anthropic/v1/messages`. Omitting it returns a native Anthropic error:
  `{"type":"error","error":{"type":"invalid_request_error","message":"anthropic-version: header is required"},"request_id":"req_…"}`.
  Any relay/proxy in front of this route must forward it.
- **Both Entra scopes work** for data-plane inference — verified 200 on
  `/anthropic/v1/messages`, `/openai/v1/chat/completions` and
  `/openai/v1/models` with tokens minted for BOTH
  `https://ai.azure.com` and `https://cognitiveservices.azure.com`.
  Prefer `https://ai.azure.com` (current Foundry docs); fall back to
  `https://cognitiveservices.azure.com` if the tenant rejects it.
- **Native Anthropic SSE streaming works**: `stream:true` returns
  `event: message_start` / `data: {...}` in exact Anthropic wire format,
  including `usage.cache_creation.ephemeral_5m_input_tokens`.
- `max_completion_tokens: 1` on the OpenAI route returns a 400
  ("Please try again with higher max_tokens") — that is a request-validation
  error, not auth. Use >= 16 in smoke tests.
- Azure Monitor metrics API **rejects comma-separated `metricnames`** for
  `Microsoft.CognitiveServices/accounts`: one metric per request.
  Working call:
  `GET https://management.azure.com{resourceId}/providers/microsoft.insights/metrics
   ?api-version=2019-07-01&metricnames=InputTokens&aggregation=Total
   &timespan=PT2H&interval=PT1H&$filter=ModelDeploymentName eq '*'`
  → `value[].timeseries[].{metadatavalues:[{name.value,value}], data:[{timeStamp,total}]}`.
  **Dimension keys come back LOWERCASED** (`modeldeploymentname`).
  Verified: the probe calls above showed up as
  `claude-sonnet-4-6 -> 8` and `gpt-5.4-nano -> 14` input tokens.
  Token metrics available: `InputTokens`, `OutputTokens`, `TotalTokens`,
  `ModelRequests`, `cacheReadInputTokens`, `ephemeral5mInputTokens`,
  `ephemeral1hInputTokens`, `TotalCalls`, `ProcessedPromptTokens`,
  `GeneratedTokens`. Dimensions on the token metrics: `ApiName`, `Region`,
  `ModelDeploymentName`, `ModelName`, `ModelVersion`.
- `GET https://management.azure.com/subscriptions/{sub}/providers/Microsoft.Consumption/budgets?api-version=2023-05-01`
  → 200 (`{"value":[]}` on this subscription).
- Azure **Retail Prices API returned 0 items** for
  `serviceName eq 'Cognitive Services' and armRegionName eq 'eastus2'`.
  Do NOT assume a price lookup works — treat per-model pricing as an
  optional, gracefully-degrading feature (config-supplied price table),
  not a hard dependency of `foundry usage`.

## Addenda (third pass — Windows subprocess encoding, MEASURED)

**Every `az` subprocess call MUST pass `encoding="utf-8", errors="replace"`.**
On Windows `az` writes console-codepage (cp1252) bytes — this user's tenant
display name is "Diretório Padrão" — and `subprocess.run(..., text=True)`
without an explicit encoding raises `UnicodeDecodeError` *inside subprocess's
reader thread*. The exception is swallowed there and `result.stdout` comes back
as **None**, so the caller fails later with a confusing `TypeError` instead of a
decode error. Also pass `shell=True` on win32 because `az` is a `.cmd` shim.

Measured token lifetimes from `az account get-access-token` (this tenant):

| resource | full lifetime |
|---|---|
| `https://ai.azure.com` | ~90 min |
| `https://cognitiveservices.azure.com` | ~72 min |
| `https://management.azure.com` | ~82 min |

`TOKEN_REFRESH_INTERVAL_SECONDS = 1800` (30 min) sits comfortably inside all
three, so the existing refresh cadence is safe. Both data-plane audiences
returned HTTP 200 against `GET {endpoint}/openai/v1/models`.
