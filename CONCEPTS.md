# AI Gateway Playground concepts

## Request path

Use **AI Gateway** to publish models and tools behind a governed endpoint.
Applications call the gateway, which applies configured controls before forwarding requests to a backend.
**Microsoft Foundry** hosts the playground's model deployments.
Configure direct backend access as a separate control; gateway policies cover traffic through the gateway.

Deploy the included templates to create an independent Azure environment.
Run the Python app over HTTPS on your computer.
Inspect captured requests and responses, then query native monitoring to confirm that Azure received the telemetry.
Validate each experiment in your deployed environment.

## Aliases and routing

Call a model alias to select a configured backend.
You can map several aliases to one catalog model or route one alias across several models.

Use an alias policy when one route needs a separate request limit.
Use a model policy for shared backend protection.
Test the combined policies before relying on their scope or enforcement.

Use priority to select a backend group, then weights to divide traffic within that group.
Lower priority numbers take precedence.
Test backend outages before relying on failover.
The playground's routing experiments check healthy model selection.

## Identity

Configure access at each connection:

| Connection | Control |
| --- | --- |
| Operator to management | Grant permission to configure and query Azure resources |
| Application to gateway | Authenticate and authorize runtime requests |
| Gateway to Foundry | Grant the gateway managed identity permission to call the owned models |
| Gateway to Learn MCP or Retail Prices | Use anonymous access to the public backend; no extra role or backend credential is needed |

Use the signed-in azd identity for management and monitoring.
Prepared clients send runtime keys to the gateway, which uses managed identity for Foundry access.
Configure user-level gateway authorization as a separate control.
Users who share a key share its identity.
Validate individual identities before enforcing individual entitlements.
Delegated OAuth requires separate implementation and validation.

## Limits and usage

Use request limits to count calls and token limits to count model usage.
Choose a supported counter key for the required scope, such as an IP or application.

Person, group, organization, and model budgets need matching attribution and enforcement.
The playground uses an IP counter; it does not implement those identity-based budgets.
Allow for overshoot in distributed enforcement.
Use separate financial controls where an exact spending cap is required.

## MCP and information controls

Use Model Context Protocol (MCP) to discover and call tools.
Run both cases from the **MCP tools** page:

| Case | Request path |
| --- | --- |
| Remote MCP | Application → owned standalone AI Gateway → Microsoft Learn MCP at `https://learn.microsoft.com/api/mcp` |
| REST API as MCP | Application → owned standalone AI Gateway → Azure Retail Prices at `https://prices.azure.com/api/retail/prices` |

Microsoft Learn provides a remote MCP server over Streamable HTTP.
The client uses `tools/list` to discover the current tools before invoking the search tool.
Tool names and schemas can change; do not assume a fixed prefix.
See the [Learn MCP overview](https://learn.microsoft.com/training/support/mcp)
and [tool discovery reference](https://learn.microsoft.com/training/support/mcp-developer-reference).

For Retail Prices, the gateway converts an OpenAPI operation into the read-only MCP tool `get_azure_retail_prices`.
The gateway calls the public REST API with GET. It needs no custom proxy.
Filter by your selected service and location with fields such as `serviceName`, `armRegionName`, and `skuName`.
The configured request uses `API Management`, `Consumption`, and your environment's selected location.
The API returns at most 1,000 records per page and supplies `NextPageLink` for further pages.
The playground displays the first 10 rows from one page and reports `hasMorePages`; it does not fetch later pages.
These public list prices do not include negotiated rates or private bills.
This tool does not calculate total monthly costs.
See the [Retail Prices API reference](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).

Neither MCP case uses a Foundry model. Model access and routing use the three owned deployments.
Both public Microsoft-hosted backends require no authentication or API key and are free to call.
The gateway still requires frontend credentials.
Inspect the real tool result and gateway credential rejections, even when a tool call returns HTTP 200.

Authorize callers as well as authenticating the gateway to the backend.
Backend OAuth alone does not propagate an end user's identity.

Use personally identifiable information (PII) redaction to remove or mask sensitive information.
Select blocking, harm filtering, or prompt-injection protection for their respective risks.
Check where each control runs and which traffic it covers.
The playground does not implement these content controls.

## Monitoring

Inspect spans for operation timing and token metrics for model usage.
Use gateway cost metrics as estimates and billing records for invoiced charges.
Use the Azure activity log for resource changes, not a record of model calls.

Review request/response content as a separate data source from metadata.
Cloud payload capture defaults to off.
Check its effective setting, access controls, and retention before enabling it.
Keep local captures distinct from cloud records.
Correlate exchanges before treating them as a complete conversation.
Captured responses do not expose hidden model reasoning.

## Limits and costs

Check availability and quota for the three configured models before deployment.
Per-user tool access and group budgets require work beyond these experiments.

Review model, gateway, and monitoring charges.
Native model cost estimates exclude other deployment charges.
Free backend calls do not remove Azure gateway or telemetry charges.
Use Reset to clear local evidence and restore routing.
Cloud resources and charges remain until you remove the environment.
Review the ownership inventory before deleting resources.

## Networking and preview support

Configure inbound private access for clients that need a private gateway address.
Configure outbound private connectivity for private model or tool backends.
Check both paths; configuring one does not configure the other.

For an on-premises model, configure a compatible endpoint, authentication, routing, and DNS.
An Azure gateway reaching that model remains distinct from a gateway hosted on premises.

The playground uses public HTTPS endpoints.
Validate private networking and other providers as separate integrations.
Check current preview limitations, availability, and pricing before deploying.
Validate the Azure integration in addition to running local tests before deciding whether it meets production requirements.

See the [AI Gateway resource guidance](https://learn.microsoft.com/azure/api-management/ai-gateway-resource) for current support and limitations.
