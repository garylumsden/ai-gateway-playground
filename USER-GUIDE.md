# Use AI Gateway Playground

Deploy the resources using the [README](README.md), then run the HTTPS app on your computer.

## Open the app

```powershell
pwsh .\scripts\start-presenter.ps1
```

Use the browser tab that the launcher authorizes.
Keep the server terminal open while you experiment.
If the certificate is untrusted, stop and complete the README certificate setup.
Do not bypass certificate validation.

Open **(?)** for page help, account details, and resource links.
Confirm the Azure CLI account and subscription before running experiments.
Keep resource links and their download private; they contain deployment identifiers.

## Explore the experiments

| Page | Action | Result |
| --- | --- | --- |
| Overview | Read the request path | Inspect the gateway, models, and monitoring connections |
| Resources | Open a resource card | Inspect your gateway, Foundry account, or monitoring resource |
| Model access | Run and select a request | Inspect the prompt, answer, model, status, latency, and tokens |
| Model routing | Run Weights or Priorities | Inspect the selected backends |
| Request limits | Run and wait for recovery | Compare a limited alias with an available normal alias |
| MCP tools | Run and inspect protocol calls | Call the model through an OpenAPI-generated MCP tool |
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

Use your Azure CLI identity for management and monitoring.
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

## MCP model tool

Call `summarize_text` in the `model` namespace of the `model-tools` server.
The gateway generates this tool from the playground's Foundry chat-completions endpoint.
Inspect session initialization, tool discovery, and invocation with the project-task prompt.

Inspect the returned answer, model name, and response token counts.
Check the answer as well as the HTTP status; an HTTP 200 can contain a tool error.
The app reads model output from `structuredContent` or a JSON text block and retains the raw envelope.
Treat empty answers, malformed data, and backend errors as failures.

Use the configured primary deployment with `max_completion_tokens: 96` and `reasoning_effort: "none"`.
Select compatible models or adapt these requests.
Delegated OAuth, remote federation, and per-user tool entitlements need separate implementation and validation.

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
| MCP tool fails | Inspect its response, model configuration, and managed identity permissions |

Use the [README cleanup guidance](README.md#limits-and-cleanup) when you finish experimenting.
