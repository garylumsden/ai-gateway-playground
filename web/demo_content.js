"use strict";

const DEMO_CONTENT = {
  start: {
    title:"Playground request path",
    explanation:"Run small experiments against one AI Gateway. The template creates the Foundry models and monitoring resources that these experiments need.",
    flow:["Local HTTPS app","AI Gateway","Owned Foundry models"],
    facts:[
      ["AI Gateway resource","Microsoft.ApiManagement/aigateways"],
      ["Gateway SKU","Consumption"],
      ["Configuration","Model aliases, structured JSON policies, and an OpenAPI-generated MCP tool"],
      ["Evidence","Recorded requests plus native gateway monitoring; validate each deployment separately"]
    ]
  },
  resources: {
    title:"One owned environment",
    explanation:"All cloud dependencies belong to the playground deployment. The Python app runs on your computer, not in an Azure web application.",
    flow:["Dedicated resource group","Gateway and Foundry account","Dedicated monitoring resources"],
    facts:[
      ["Gateway","AI Gateway Consumption with managed identity"],
      ["Models","Three configurable Foundry deployments"],
      ["Monitoring","Log Analytics workspace and Application Insights"],
      ["Permissions","Scoped role assignment for gateway access to its Foundry account"]
    ]
  },
  model: {
    title:"Authentication and model selection",
    explanation:"The client supplies a gateway key and an alias in the model field. The gateway authenticates to Foundry with managed identity.",
    flow:["Client: gateway key","Model alias","Foundry: managed identity"],
    facts:[
      ["Route","POST /default/models/openai/v1/chat/completions"],
      ["Client credential","Gateway access key; not an Entra user token"],
      ["Backend role","Cognitive Services OpenAI User at the Foundry account"],
      ["Checks","Missing key, invalid key, then three valid model replies"]
    ]
  },
  routing: {
    title:"Alias routing",
    explanation:"Lower priority numbers select the preferred backend group. Weights divide requests among backends within that group.",
    flow:["One client-facing alias","Priority group","Weighted model selection"],
    facts:[
      ["Backends","Three owned, configurable OpenAI-compatible deployments"],
      ["Weights mode","Priority 0 for all models; relative weights 50 / 30 / 20"],
      ["Priority mode","Third model at priority 0; the other models at priority 1"],
      ["Evidence","Returned model names from 50 weighted or 10 priority requests"]
    ]
  },
  rate: {
    title:"Alias policy and shared protection",
    explanation:"The normal and limited aliases share one catalog model. Only the limited alias has the request-rate policy.",
    flow:["Client IP","Alias request limit","Shared model token protection"],
    facts:[
      ["Request policy scope","Limited alias"],
      ["Configured request limit","1 request / 30 seconds, counted by IP"],
      ["Control route","Normal alias must work during the limited alias's rejection"],
      ["Important limit","Distributed enforcement can overshoot; this is not a financial cap"]
    ]
  },
  mcp: {
    title:"A model call exposed as an MCP tool",
    explanation:"The gateway generates a summarization tool from an OpenAPI operation over the playground's own Foundry chat-completions endpoint.",
    flow:["MCP client: runtime key","model-tools: model / summarize_text","Foundry: managed identity"],
    facts:[
      ["Protocol sequence","initialize, notifications/initialized, tools/list, tools/call"],
      ["Tool definition","Server model-tools; namespace model; operation summarize_text"],
      ["Backend operation","POST /openai/v1/chat/completions"],
      ["Backend audience","https://cognitiveservices.azure.com"],
      ["Success condition","Successful tool invocation with a non-empty model answer and returned model name"],
      ["Identity boundary","Runtime key to gateway; managed identity to Foundry; no delegated OAuth"]
    ]
  },
  activity: {
    title:"Native request evidence",
    explanation:"A span records an operation and its timing. The gateway exports spans through OpenTelemetry Protocol (OTLP); Server spans identify gateway requests.",
    flow:["Gateway Server span","Native OTLP exporter","OTelSpans in Log Analytics"],
    facts:[
      ["Request source","OTelSpans, Kind == Server"],
      ["Scope","New Application Insights component"],
      ["Local app spans","AppDependencies / ai-gateway-playground-presenter"],
      ["Timing","Ingestion can lag; request success and telemetry arrival are separate checks"]
    ]
  },
  tokens: {
    title:"Native usage counters",
    explanation:"Read the gateway's native token and cost metrics. Response usage stays separate in the request history.",
    flow:["Gateway token / cost metrics","Managed-identity export","Native monitoring"],
    facts:[
      ["Metric","azure.ai_gateway.client.token.usage"],
      ["Cost metric","azure.ai_gateway.client.token.cost"],
      ["Query","PromQL; latest cumulative samples seen within one hour"],
      ["Interpretation","Gateway-estimated model cost, not hourly spend, billed charges, or the gateway service charge"]
    ]
  },
  monitoring: {
    title:"Monitoring sources",
    explanation:"AI Gateway exports native model request spans, token counters, and estimated model costs. Local app spans remain a separate source.",
    flow:["AI Gateway","Native signals","Azure Monitor"],
    facts:[
      ["Requests","OTelSpans / Server spans"],
      ["Tokens","azure.ai_gateway.client.token.usage"],
      ["Resource changes","Azure activity log"],
      ["Conversation boundary","Cloud payload capture defaults to off; access and retention need separate review"]
    ]
  },
  setup: {
    title:"Independent checks and partial results",
    explanation:"Each stage records its own outcome. A failure stops dependent work and marks the remaining stages Skipped.",
    flow:["Access, limits, and MCP","Native activity and both routing modes","Native token reporting"],
    facts:[
      ["Stages","model-access, request-limits, ai-gateway-mcp, native-activity, routing-priority, routing-weighted, native-tokens"],
      ["Failure evidence","Failed stage, error, and the associated request/response"],
      ["Expected rejection","A deliberate 401/403 or rate-limit response can be a successful check"],
      ["Scope","Routing tests update playground-routing; a native token gap is not a full pass"]
    ]
  }
};

const DEMO_REFERENCE = [
  {title:"Identity and policy scope",rows:[
    ["Experiment","Portal / management","Azure CLI user and Entra permissions manage the playground resources."],
    ["Experiment","Runtime authentication","Model and MCP calls use runtime keys. The gateway accesses Foundry with managed identity."],
    ["Experiment","Alias policies","Independent request limits on aliases that use the same catalog model."],
    ["Not demonstrated","Person / group budgets","The rate example uses an IP counter. It does not map Entra groups to personal token allowances."],
    ["Not configured","PII redaction","Request/response capture does not redact PII. Apply separate redaction and access controls where required."]
  ],source:"https://learn.microsoft.com/azure/api-management/ai-gateway-govern-secure-assets"},
  {title:"Networking and providers",rows:[
    ["Experiment","Network path","The playground calls public HTTPS endpoints."],
    ["Documented preview","Private networking","Private Link and outbound virtual network integration require separate configuration and validation."],
    ["Experiment","Model routing","Three owned Foundry models use one alias. Priority selects a group; weight divides traffic inside it."],
    ["Not configured","Other providers / on-premises","No Bedrock, other-provider, or on-premises backend is configured in this deployment."],
    ["Not demonstrated","Outage failover","The priority sample tests healthy-backend preference without injecting an outage."]
  ],source:"https://learn.microsoft.com/azure/api-management/ai-gateway-configure-private-networking"},
  {title:"Observability and MCP",rows:[
    ["Requires validation","Native evidence","Run the native checks to confirm that gateway spans, token counters, and estimated model cost arrive in Azure Monitor."],
    ["Experiment","Local payload viewer","The app shows sanitized request/response bodies separately from cloud payload capture."],
    ["Configuration required","Conversation review","Cloud payload capture defaults to off. Review access and retention before enabling it; hidden model reasoning is not exposed."],
    ["Experiment","Generated MCP tool","OpenAPI-to-MCP exposes summarize_text over the playground's own Foundry model. Inspect the returned answer and response token counts."],
    ["Not demonstrated","Delegated MCP OAuth","The prepared MCP client uses a gateway key, not delegated user OAuth."]
  ],source:"https://learn.microsoft.com/azure/api-management/ai-gateway-manage-models-tools"},
  {title:"SKU, API version, and preview",rows:[
    ["Template target","AI Gateway","Consumption on Microsoft.ApiManagement/aigateways. Deploy and validate your own environment."],
    ["Template contract","API versions","Provisioning: 2026-09-01-preview. Models: 2026-05-01. Alias policies / OTLP: 2025-09-01-preview."],
    ["Preview","Production commitments","Confirm current availability, limits, pricing, SLA, and GA plans with the product team."]
  ],source:"https://learn.microsoft.com/azure/api-management/ai-gateway-resource"}
];

function capturedBody(body) {
  if (!body) return {value:null,notice:"Awaiting response..."};
  if (body.truncated) return {value:null,notice:"The capture is truncated. Open Full JSON to inspect the recorded portion."};
  if (body.format !== "json") return {value:body.text,notice:""};
  try {
    return {value:JSON.parse(body.text),notice:""};
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return {value:null,notice:"The captured body is not valid JSON. Open Full JSON to inspect it."};
  }
}

function contentText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map(part => typeof part?.text === "string" ? part.text : "").filter(Boolean).join("\n");
}

function modelAnswer(value) {
  return [
    ...(Array.isArray(value?.choices) ? value.choices.map(choice => contentText(choice?.message?.content)) : []),
    ...(Array.isArray(value?.output) ? value.output.map(item => contentText(item?.content)) : []),
  ].filter(Boolean).join("\n\n");
}

function toolText(result) {
  const text = contentText(result?.content);
  let value = result?.structuredContent;
  if (value == null && text) {
    try {
      value = JSON.parse(text.replace(/^\uFEFF/, ""));
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      return text;
    }
  }
  return modelAnswer(value) || (typeof value === "string" ? value : value != null ? JSON.stringify(value, null, 2) : text);
}

function exchangeOverview(exchange) {
  const request = capturedBody(exchange.requestBody);
  const response = capturedBody(exchange.responseBody);
  const input = request.value;
  const output = response.value;
  const view = {
    inputTitle:"Input",outputTitle:"Response",
    input:request.notice,output:response.notice,
  };
  if (input && typeof input === "object" && (Array.isArray(input.messages) || "input" in input)) {
    view.inputTitle = "Input prompt";
    view.outputTitle = "Model answer";
    const messages = input.messages || (Array.isArray(input.input) ? input.input : []);
    view.input = typeof input.input === "string" ? input.input : messages
      .filter(message => !["system","developer"].includes(message.role))
      .map(message => contentText(message.content)).filter(Boolean).join("\n\n");
    view.output = modelAnswer(output) || response.notice || "The response contains no model answer. Inspect Full JSON.";
  } else if (input && typeof input === "object" && input.method) {
    view.inputTitle = "MCP request";
    view.outputTitle = "Tool response";
    if (input.method === "tools/call") {
      const argumentsText = Object.entries(input.params?.arguments || {}).map(([key,value]) => {
        const text = key === "messages" && Array.isArray(value)
          ? value.filter(message => !["system","developer"].includes(message.role)).map(message => contentText(message.content)).filter(Boolean).join("\n\n")
          : typeof value === "object" ? JSON.stringify(value) : value;
        return `${key.replace(/([a-z])([A-Z])/g,"$1 $2")}: ${text}`;
      });
      view.input = [`Tool: ${input.params?.name || "not supplied"}`,...argumentsText].join("\n");
      view.output = toolText(output?.result) || response.notice || "The tool returned no readable content. Inspect Full JSON.";
    } else {
      view.input = {
        initialize:`Initialize the MCP session.\nProtocol: ${input.params?.protocolVersion || "not supplied"}`,
        "notifications/initialized":"Confirm MCP session initialization.",
        "tools/list":"List the permitted tools.",
      }[input.method] || input.method;
      view.output = Array.isArray(output?.result?.tools)
        ? output.result.tools.map(tool => [tool.name,tool.description].filter(Boolean).join("\n")).join("\n\n")
        : output?.result?.protocolVersion ? `Session initialized.\nProtocol: ${output.result.protocolVersion}`
        : exchange.encoding === "empty" ? `The server accepted the notification (HTTP ${exchange.httpStatus}).`
        : response.notice || "Inspect Full JSON for the protocol response.";
    }
  } else if (input && typeof input === "object" && typeof input.query === "string") {
    view.inputTitle = "Monitoring query";
    view.outputTitle = "Query result";
    view.input = input.query;
    view.output = Array.isArray(output?.tables) ? output.tables.map(table =>
      (table.rows || []).map(row => table.columns.map((column,index) => `${column.name}: ${row[index]}`).join("\n")).join("\n\n")
    ).join("\n\n") : response.notice || "Inspect Full JSON for the metric series.";
  } else if (["Native token metrics","AI Gateway metrics"].includes(exchange.target)) {
    view.inputTitle = "Monitoring query";
    const cost = exchange.path.includes("azure.ai_gateway.client.token.cost");
    view.outputTitle = cost ? "Native estimated cost samples" : "Native metric samples";
    view.input = new URLSearchParams(exchange.path.split("?")[1] || "").get("query") || exchange.path;
    view.output = Array.isArray(output?.data?.result) ? output.data.result.map(series => [
      series.metric?.["gen_ai.request.model"],
      series.metric?.["gen_ai.token.type"],
      series.value?.[1],
      ...(cost && series.metric?.["azure.ai_gateway.currency"] ? [series.metric["azure.ai_gateway.currency"].toUpperCase()] : []),
    ].filter(value => value !== undefined).join(" / ")).join("\n") : response.notice || "Inspect Full JSON for the metric response.";
  } else {
    view.input = typeof input === "string" ? input : request.notice || "No request body.";
    view.output = typeof output === "string" ? output : response.notice || "Inspect Full JSON for the response.";
  }
  if (exchange.httpStatus >= 400 || exchange.error || exchange.validationError || output?.error || output?.result?.isError) {
    const error = output?.error || output?.result?.structuredContent?.error;
    const message = typeof error === "string" ? error : error?.message || error?.code;
    view.outputTitle = exchange.expected ? "Expected rejection" : "Request outcome";
    view.output = exchange.validationError || message || exchange.error
      || (output?.result?.isError ? toolText(output.result) || "The tool reported an error. Inspect Full JSON." : `The gateway returned HTTP ${exchange.httpStatus}.`);
  }
  return view;
}
