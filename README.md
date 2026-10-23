# AI Gateway Playground

Use a local HTTPS app to test AI Gateway model access, routing, policies, and MCP tools.
Inspect requests and answers alongside native monitoring.
Deploy the included templates to create a Foundry account, three model deployments, and the gateway's monitoring resources.
You need no existing application, gateway, or workspace.

This independent project is not an official Microsoft product.
Check the [AI Gateway resource guidance](https://learn.microsoft.com/azure/api-management/ai-gateway-resource) before deploying.
Preview APIs and capabilities can change; validate them in your Azure environment.

## Features

- Test valid credentials and missing/invalid-key rejections.
- Apply different request limits to aliases that share one catalog model.
- Route one alias across three models using weights or priorities.
- Call a Foundry model through an OpenAPI-generated MCP tool.
- Query native request spans, token counters, and estimated model cost.
- Read prompts and answers, then expand the HTTP and JSON details.
- Inspect stage failures and skipped work.
- Reset local results and restore weighted routing.

Use the fixed prompts for support tickets, project access controls, document quality, and task summaries.
The app accepts predefined actions; it has no arbitrary command or destination input.
The Python backend keeps gateway keys and Azure tokens out of the browser.

## Requirements

- Windows, PowerShell 7, Python 3.11 or later, Azure CLI, and Bicep.
- An Azure subscription with access to AI Gateway Consumption and sufficient model quota.
- A selected Azure location that supports the gateway and all three configured model deployments.
- An authenticated Azure CLI user with deployment, role-assignment, resource-read, and monitoring permissions.
- An existing trusted, exportable `CN=localhost` certificate in the current Windows user's certificate store.

Prepare the CLI session and certificate before starting.
The scripts preserve the selected subscription and certificate trust; they do not sign you in or create certificates.
Stop if certificate validation fails.

## Configure an environment

```powershell
New-Item -ItemType Directory -Path .azure -Force | Out-Null
Copy-Item config.example.json .azure\config.json
```

Replace the placeholders in `.azure\config.json` with your subscription, location, and resource names.
Choose a dedicated resource group.
Keep the configuration and generated `.azure\state.json` out of Git.

| Setting | Purpose |
| --- | --- |
| `subscriptionId` | Target Azure subscription |
| `location` | Selected Azure location |
| `resourceGroupName` | Dedicated resource group owned by this playground |
| `deploymentName` | Subscription deployment name |
| `modelDeployments` | Three owned deployments; each entry sets `name`, `model`, `version`, `sku`, and `capacity` |
| `model` | Primary deployment name for normal and limited aliases and the MCP model tool |
| `routingModels` | The three configured deployment names used by the routing alias |
| `allowNativeTokenGap` | Keep `false` to require native token reporting |

Select supported model names, versions, SKUs, and capacities for your subscription.
Use three distinct returned model names so you can identify each backend in routing results.
The prepared requests require `reasoning_effort: "none"` and `max_completion_tokens`.
Choose compatible chat-completions models or adapt the requests.
Check the example models against current availability and quota before deployment.

## Deploy

Run from the repository root:

```powershell
python .\scripts\gateway_demo.py preflight
python .\scripts\gateway_demo.py preview
python .\scripts\gateway_demo.py deploy
python .\scripts\gateway_demo.py configure
python .\scripts\gateway_demo.py configure-mcp
python .\scripts\gateway_demo.py configure-routing
```

Use the template to create a dedicated resource group containing Foundry models, a Log Analytics workspace, AI Gateway, and Application Insights.
Deployment grants the gateway's managed identity scoped model access.
Run the configure commands to create aliases, policies, telemetry export, the MCP tool, and routing.
Inspect preflight and preview results before committing to the deployment and its charges.

The current resource type is `Microsoft.ApiManagement/aigateways`, with the `Consumption` SKU.
Provisioning uses `2026-09-01-preview`.
Model assets use `2026-05-01`; alias policies and exporters use `2025-09-01-preview`.
The template suppresses BCP081 because the bundled Bicep catalog lacks the gateway type.
Run Azure validation before deployment.

Setup configures native OpenTelemetry Protocol (OTLP) export to Application Insights, including monitoring access.
Cloud payload capture defaults to `false`.
Review privacy, access, and retention requirements before enabling request/response capture.

## MCP model tool

Call `summarize_text` in the `model` namespace of the `model-tools` MCP server.
The tool calls `POST /openai/v1/chat/completions` on the playground's Foundry account using your configured `model` deployment.
You need no separate tool-hosting service.

The prepared `tools/call` arguments are:

```json
{
  "model": "<configured-primary-deployment>",
  "messages": [
    {
      "role": "user",
      "content": "Summarize: A project has three open tasks. Two need owners and one needs a due date. State the next action in one sentence."
    }
  ],
  "max_completion_tokens": 96,
  "reasoning_effort": "none"
}
```

Inspect session initialization, tool discovery, and invocation in the request history.
Read the answer from the returned chat-completions JSON in `structuredContent` or a JSON text block.
Expand **Full JSON and HTTP details** to inspect the captured envelope.
Check the tool result as well as the HTTP status; an HTTP 200 can contain a tool error.

Use gateway runtime keys for prepared model and MCP calls.
The gateway uses managed identity with audience `https://cognitiveservices.azure.com` for Foundry access.
Delegated OAuth, per-user tool entitlements, and remote MCP federation need separate implementation and validation.

## Start the app

Create an isolated Python environment and install the pinned dependencies from your approved package source:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-presenter.lock.txt
pwsh .\scripts\start-presenter.ps1
```

Use the authorized browser tab that the launcher opens.
Keep the terminal open while using the app.
The server binds to `127.0.0.1` and serves HTTPS at its `localhost` URL.
Supply `-Port` to choose a port instead of using an available one.

The app protects its launch capability with Windows user-scoped protection.
It uses Secure, HttpOnly, SameSite=Strict cookies, same-origin checks, and CSRF protection.
This authorization covers the local app; hosted browser Entra authentication requires a different design.
The server lasts eight hours, and browser authorization lasts two hours.
Run the launcher again to renew browser authorization.

Use the [user guide](USER-GUIDE.md) for the experiment sequence.
The **(?)** panel contains operator details and page notes.
Keep resource links and their download private because they contain deployment identifiers.

## Monitoring and evidence

Inspect these native gateway signals:

| Signal | Source | Interpretation |
| --- | --- | --- |
| Gateway requests | Component-scoped `OTelSpans` with Server kind | Request activity, status, duration, and trace identifiers |
| Token usage | `azure.ai_gateway.client.token.usage` | Latest cumulative samples seen within one hour |
| Model cost | `azure.ai_gateway.client.token.cost` | Gateway-estimated model cost with the native currency label |
| Local app operations | `AppDependencies`, role `ai-gateway-playground-presenter` | Local command spans, separate from gateway traffic |

Read token and cost samples as cumulative counters, not hourly spend or billing-period totals.
Native cost estimates exclude gateway service charges; use billing records for invoiced charges.
The app excludes overlapping total/component series from its sums and shows missing-price flags.
Treat **Not reported** as missing cost data, not zero cost.
Configuration changes can reset these counters.

The app holds request/response evidence in server memory for the current session.
The runner emits JSON-line events after redacting credentials, known deployment identifiers, session headers, and email addresses.
It captures up to 180 exchanges, 16,384 characters per body, and 768,000 body characters per run.
Check truncation notices before treating a body as complete.
The request history excludes Azure management responses.
Configure cloud payload capture as a separate gateway setting.

## Limits and cleanup

Use the rate experiment to test an IP counter, which can overshoot its configured request count.
Implement person/group allowances and financial caps as separate controls.
Test backend outages before relying on failover; the routing experiments check healthy model selection.
The MCP check requires a non-empty model answer from a successful tool call.

Run **Check Setup** to check access, limits, MCP, both routing modes, and native activity/token reporting.
Treat a tolerated native token gap as a partial result.

Use Reset to clear local results and restore weighted routing.
Reset preserves Azure resources, stored logs, and service-side quotas.

```powershell
python .\scripts\gateway_demo.py inventory
```

Review the ownership inventory before deleting the dedicated environment.
This repository provides no `azd` project or `azd down` workflow.
Decide which resources and stored payload data to remove before starting cleanup.

### Costs

Review gateway, model, monitoring ingestion, and retention prices before deployment.
Check quota and charges for the selected model SKUs and capacities.
Weighted routing sends 50 requests per sample; priority routing sends 10.
Check Setup runs both modes alongside the other experiments.
Additional runs can increase usage.

Closing the app or selecting Reset leaves cloud resources in place and does not stop charges.
Use Azure Cost Management and billing records to review those charges.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
node --test tests\test_exchange_content.cjs
node --check web\app.js
node --check web\demo_content.js
az bicep build --file infra\main.bicep --stdout
```

Run the HTTPS checks against a configured, running app:

```powershell
$env:PRESENTER_E2E = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
Remove-Item Env:PRESENTER_E2E
```

The opt-in HTTPS checks skip without an Azure environment.
Validate the Azure integration in a deployed environment in addition to running local unit tests.

## Documentation

- [User guide](USER-GUIDE.md)
- [AI Gateway concepts](CONCEPTS.md)
- [Microsoft AI Gateway documentation](https://learn.microsoft.com/azure/api-management/ai-gateway-resource)
- [Azure AI Gateway samples](https://github.com/Azure-Samples/AI-Gateway)
- [AI Gateway portal documentation](https://ai.gateway.azure.com/docs)

The implementation follows Microsoft documentation and Azure sample patterns for gateway authentication and telemetry.
It uses the native portal's contract for alias routing and cost metrics.
Keep recordings, evidence exports, configuration, certificates, and resource-link downloads out of Git.

## Related project

[AI Observability Demo](https://github.com/gjlumsden/ai-observability-demo) covers a separate implementation.
You can use this playground without deploying that project.

## License

[MIT](LICENSE).
