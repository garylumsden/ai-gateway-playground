# Use AI Gateway Playground

Deploy the resources using the [README](README.md), then run the HTTPS app on your computer.

## Open the app

```powershell
pwsh .\scripts\start-presenter.ps1
```

Use the browser tab that the launcher authorizes.
Keep the server terminal open while you experiment.
If the certificate is untrusted, stop and check the README certificate prerequisite.
Do not bypass certificate validation.

Open **(?)** for page help, account details, and resource links.
Confirm the signed-in azd account and selected environment before running experiments.
Keep resource links and their download private; they contain deployment identifiers.

## Explore the experiments

| Page | Action | Result |
| --- | --- | --- |
| Overview | Read the request path | Inspect the gateway, models, and monitoring connections |
| Resources | Open a resource card | Inspect your gateway, Foundry account, or monitoring resource |
| Model access | Run and select a request | Inspect the prompt, answer, model, status, latency, and tokens |
| Model routing | Run Weights or Priorities | Inspect the selected backends |
| Request limits | Run and wait for recovery | Compare a limited alias with an available normal alias |
| MCP tools | Run and inspect both cases | Search Microsoft Learn through remote MCP and query retail prices through native OpenAPI-to-MCP conversion |
| Monitoring | Refresh monitoring | Read native activity, token usage, and estimated model cost |
| Gateway activity | Run | Query native Server spans |
| Token usage and cost | Run | Inspect cumulative counters and cost by alias/model |
| Check Setup | Run | Check all experiment paths in sequence |

Select a captured request to inspect its prompt and answer.
Expand **Full JSON and HTTP details** for headers and protocol bodies.
Expand **Request history** to view the captured exchanges.
Select **Latest request** to follow new calls again.

## Model access

Run the model check against `playground-model`.
Expect three project answers and rejections for missing/invalid credentials.
Treat rejections as expected for those two credential checks.
A rejected request with valid credentials is a failure.

Use your signed-in azd identity for management and monitoring.
The prepared client sends runtime keys to the gateway, which uses managed identity for Foundry access.
Hosted browser identity and group-based model access require separate implementation.

## Model routing

The `playground-routing` alias uses the three configured model deployments.

**Apply weights + run 50** sets all priorities to 0 and weights to 50 / 30 / 20.
Expect variation in finite sample counts.

**Apply priorities + run 10** prefers the third model at priority 0.
The other models have priority 1.
Lower priority numbers take precedence; weights apply within that group.
Test backend outages before relying on failover.
This experiment checks healthy model selection.

Select a tab to choose the next run.
Read **Last applied** to check the saved gateway mode.
Run the selected action to apply it in Azure.

## Request limits

Run the limit check against `playground-limited`, which shares the normal alias's catalog model.
Inspect the rejection, the working normal-alias request, and recovery.
Allow for IP-counter overshoot; this check cannot establish personal allowances or an exact spending cap.

## MCP tools

Select **Run** on the **MCP tools** page to run both cases through your standalone AI Gateway.
Inspect session initialisation, `tools/list` discovery, and tool invocation for each case.
Review the returned data and the missing/invalid credential checks.
Expect the gateway to reject those two credential checks even though both backends accept anonymous calls.
Treat a rejection with valid gateway credentials as a failure.

### Remote Microsoft Learn MCP

The gateway forwards MCP calls to `https://learn.microsoft.com/api/mcp` over Streamable HTTP.
Inspect the discovered search tool name and its result links.
The application uses tool discovery rather than assuming a fixed tool-name prefix.
Microsoft Learn also offers article fetching and code sample search tools.
See the [Microsoft Learn MCP overview](https://learn.microsoft.com/training/support/mcp)
and [developer reference](https://learn.microsoft.com/training/support/mcp-developer-reference).

### Azure Retail Prices REST API as MCP

The gateway uses native OpenAPI-to-MCP conversion to expose `get_azure_retail_prices`.
It calls `https://prices.azure.com/api/retail/prices` with a read-only GET request.
No custom proxy or Foundry model sits between the gateway and this API.

The configured request filters for service `API Management`, SKU `Consumption`, and your environment's selected location.
Choose the configured service and location filters for the prices you need.
The API supports `$filter` fields such as `serviceName`, `armRegionName`, and `skuName`.
The page displays the first 10 rows from one API page.
Inspect the returned price, currency, unit of measure, tier minimum, and effective date.
Each API page contains at most 1,000 records. A non-empty `NextPageLink` means more records remain.
Check `hasMorePages` in the captured result. This run does not fetch the remaining pages.
Do not treat the displayed rows or one page as the full price catalogue.
See the [Retail Prices API reference](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).

These are public retail list prices. They do not give access to negotiated rates, private billing data,
or a total monthly cost calculation.

For both cases, inspect the tool result as well as the HTTP status.
An HTTP 200 can contain a tool error.
Treat empty results, malformed data, and backend errors as failures.
Both Microsoft-hosted backends require no authentication or API key and are free to call.
The gateway still enforces frontend credentials. Azure gateway, model, and telemetry usage remains billable.
Neither MCP case calls a Foundry model; the model access and routing pages retain the owned model deployments.
Delegated OAuth and per-user tool entitlements need separate implementation and validation.

## Monitoring and cost

Use **AI Gateway monitoring** for the native portal views.
Use **Workspace Logs** for component-scoped span queries.
The Azure activity log records resource changes.
Local app spans remain separate from gateway Server spans.

Read `azure.ai_gateway.client.token.usage` and `azure.ai_gateway.client.token.cost` as cumulative samples seen within one hour.
The view does not calculate hourly spend or billing-period totals.
Native model cost estimates exclude the gateway service charge.
Check missing-price flags before treating an estimate as complete.
Use billing records for invoiced charges.
Configuration changes can reset cumulative counters.

Cloud payload capture defaults to `false`.
Check its effective setting, access controls, and retention before enabling it.
Inspect local exchanges and cloud captures as separate evidence sources.
Neither exposes hidden model reasoning.

Review model, gateway, and monitoring charges after experimenting.
Closing the app or selecting Reset leaves cloud resources in place.

## Check Setup and reset

Run **Check Setup** for model access, request limits, MCP, both routing modes, and native activity/token checks.
Allow several minutes for requests, configuration propagation, and telemetry ingestion.
Check the per-stage results.
After a failure, earlier results remain visible and unexecuted stages show **Skipped**.
Treat a tolerated token-reporting gap as a partial result.
The routing checks finish in weighted mode.

Select **Reset**.
Review the confirmation before continuing.
Allow 30 seconds for routing propagation.
You return to Overview with cleared local results/history and weighted routing restored.
Reset preserves Azure resources and stored logs.
Wait for a running check to finish before resetting.

## Troubleshooting

| Problem | Action |
| --- | --- |
| Browser authorization expired | Run the launcher again |
| CLI user changed | Correct the CLI session and restart the app |
| Gateway portal opens onboarding | Check the account, directory, and selected gateway |
| Another check is running | Wait or refresh to reconnect |
| Telemetry is delayed | Generate model traffic and allow the native check to wait |
| Cost is not reported | Inspect native query results and missing-price flags; do not assume zero |
| MCP tool fails | Inspect discovery, the tool result, gateway credentials, and public backend availability |

Use the [README cleanup guidance](README.md#clean-up) when you finish experimenting.
