"use strict";

const STEPS = [
  {id:"start",label:"Overview",title:"AI Gateway Playground",description:"Explore model access, routing, request limits, MCP tools, and native monitoring in one owned environment.",action:null,
    note:"Follow the experiment path. Inspect each request, model answer, and native monitoring result.",
    caution:"Deploy and configure your own resources first. Local tests do not prove that an Azure deployment works.",links:["newGateway","foundry","newGroup"]},
  {id:"resources",label:"Resources",title:"Resources",description:"Open the playground's gateway, Foundry account, and monitoring resources.",action:null,
    note:"Open a resource card to inspect its Azure settings. Resource names and the complete links download are below.",
    caution:"Check the browser account before opening portal links. Gateway blades use the portal's selected gateway; confirm the gateway name below.",links:["newGroup","foundry","workspace"]},
  {id:"model",label:"Model access",title:"Model access",description:"Send model requests with valid, missing, and invalid credentials.",action:"smoke",
    note:"Use the request selector to show three scenarios and their answers. Show the two credential rejections. Expand Full JSON only for API details.",
    caution:"This example uses a gateway key, not an Entra user token. The gateway uses managed identity for Foundry.",links:["gatewayModels","gatewayAccess","newGateway","foundry"]},
  {id:"routing",label:"Model routing",title:"Model routing",description:"Route one alias to three models using weights and priorities.",action:"routing-weighted",
    note:"Select a mode, then Run to apply it to the dedicated alias. Weights measures 50 requests across the 50/30/20 configuration. Priorities prefers the third model at priority 0 over the others at priority 1.",
    caution:"Lower priority numbers take precedence. Weights split traffic within a priority group. The test waits 30 seconds before sampling. This does not simulate an outage or prove failover.",links:["gatewayRouting","gatewayModels","newGateway"]},
  {id:"rate",label:"Request limits",title:"Request limits",description:"Test a limited route and an unrestricted route.",action:"rate",
    note:"Show the 429 response, the working unrestricted route, and recovery after the retry window.",
    caution:"The limit uses an IP counter and can overshoot. It does not demonstrate person or group allowances, or a spending cap.",links:["gatewayPolicies","gatewayModels","newGateway"]},
  {id:"mcp",label:"MCP tools",title:"MCP tools",description:"Call an OpenAPI-generated tool that summarizes text with the playground's own Foundry model.",action:"mcp",
    note:"Inspect initialization, tool discovery, and invocation. Show the returned model answer, model name, and response token counts.",
    caution:"The client uses a runtime key. The gateway uses managed identity for Foundry. This example does not demonstrate delegated OAuth or remote MCP federation.",links:["gatewayMcp","foundry","newGateway"]},
  {id:"monitoring",label:"Monitoring",title:"Monitoring",description:"Review request activity, token usage, estimated model cost, and policy outcomes.",action:"monitoring",
    note:"Select Refresh monitoring for native requests, token counters and the gateway's estimated model cost. Open Token usage for the alias/model cost breakdown.",
    caution:"Costs come from the gateway's native metric, not a local price calculation or Azure billing. The activity log records resource changes, not every model call.",links:["gatewayMonitoring","newInsights","workspace","gatewayAudit"]},
  {id:"activity",label:"Gateway activity",title:"Gateway activity",description:"View AI Gateway requests in Application Insights.",action:"activity",
    note:"Open Application Insights or Workspace Logs to inspect the playground's native request records.",
    caution:"Ingestion can take five minutes. Local app spans use AppDependencies and remain separate from native gateway spans.",links:["gatewayMonitoring","newInsights","workspace"]},
  {id:"tokens",label:"Token usage and cost",title:"Token usage and cost",description:"Review native token counters and the estimated model cost recorded by AI Gateway.",action:"telemetry",
    note:"Show the gateway's native token and cost metrics, then the alias/model cost breakdown. Use AI Gateway monitoring for its period-based views.",
    caution:"These are latest cumulative samples seen within one hour, not hourly spend or a billing-period total. Cost estimates exclude the gateway service charge. Configuration changes can reset counters.",links:["gatewayMonitoring","newInsights"]},
  {id:"setup",label:"Check Setup",title:"Check Setup",description:"Check model access, request limits, MCP tools, both routing modes, and native monitoring.",action:"verify",
    note:"Run this check after configuration. Each stage records its own result and keeps the associated request evidence.",
    caution:"A failed stage stops dependent work. A native token gap is not a full pass. These checks generate model traffic and change the routing alias.",links:["newGateway","foundry","newInsights"]}
];
const STATUS = {"not-run":"Not run",running:"Running",passed:"Passed","known-gap":"Token reporting failed",failed:"Failed"};
const LINK_LABELS = {
  newGateway:"AI Gateway", gatewayPortal:"AI Gateway portal", newInsights:"Application Insights",
  newGroup:"Playground resource group",
  workspace:"Workspace Logs", foundry:"Foundry account",
  gatewayAudit:"AI Gateway activity log"
};
const GATEWAY_VIEWS = {
  gatewayModels:{label:"Model aliases",path:"/models/aliases"},
  gatewayRouting:{label:"Routing alias",path:"/catalog/models/playground-routing"},
  gatewayAccess:{label:"Gateway access",path:"/access"},
  gatewayPolicies:{label:"Gateway policies",path:"/governance/policies"},
  gatewayMcp:{label:"Model MCP server",path:"/tools/mcp-servers/model-tools"},
  gatewayMonitoring:{label:"AI Gateway monitoring",path:"/monitoring/overview"}
};
const $ = id => document.getElementById(id);
let context, csrf = "", selected = 0, activeJob = null, submitting = null, routingMode = "weighted";
let pageView = "demo", technicalKey = "", stagesKey = "", ledgerJob = "", detailKey = "", traceQuery = "", navKey = "";
const evidenceSelections = new Map(), ledgerRows = new Map();
const results = new Map();
const number = new Intl.NumberFormat();
const presentationChannel = "BroadcastChannel" in window ? new BroadcastChannel("ai-gateway-playground") : null;

async function api(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options, headers:{
    "Content-Type":"application/json", ...(options.method ? {"X-CSRF-Token":csrf} : {}), ...options.headers
  }});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "The request failed.");
  return value;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function linkButton(resource, label = LINK_LABELS[resource.id] || resource.label) {
  const anchor = el("a", "", label);
  anchor.href = resource.href;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  if (label === "Open") anchor.setAttribute("aria-label", "Open " + (LINK_LABELS[resource.id] || resource.label));
  return anchor;
}

function metric(label, value, style = "") {
  const card = el("article", "metric " + style);
  card.append(el("strong", "", typeof value === "number" ? number.format(value) : String(value ?? "Unavailable")));
  card.append(el("span", "", label));
  $("result-cards").append(card);
}

function latest(action) { return results.get(action); }

function duration(ms) {
  return typeof ms === "number" ? ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s` : "N/A";
}

function money(value, currency) {
  return typeof value === "number" ? new Intl.NumberFormat(undefined, {
    style:"currency",currency,currencyDisplay:"code",minimumFractionDigits:4,maximumFractionDigits:6,
  }).format(value) : "Not reported";
}

function renderCostMetrics(costs, details = false) {
  if (!costs?.Available) {
    metric("Estimated model cost", "Not reported", "warn text");
    if (details) {
      $("cost-panel").hidden = false;
      $("cost-notice").textContent = costs?.Reason || "Run the check to retrieve the gateway's native cost metric.";
    }
    return;
  }
  for (const total of costs.Currencies) {
    metric(total.Complete ? "Estimated model cost" : "Estimated model cost *",
      money(total.TotalCost,total.Currency),total.Complete ? "good text" : "warn text");
  }
  if (!details) return;
  $("cost-panel").hidden = false;
  $("cost-notice").textContent = costs.MissingPrices
    ? "* AI Gateway flags missing prices for some token categories. These reported estimates may be incomplete."
    : "Latest cumulative cost samples seen within one hour. These are not hourly spend, the gateway service charge, or billed charges.";
  for (const model of costs.Models) {
    const row = el("tr");
    row.append(el("th","",model.Alias),el("td","",model.Model),
      el("td","",money(model.InputCost,model.Currency)),el("td","",money(model.OutputCost,model.Currency)),
      el("td","",money(model.TotalCost,model.Currency) + (model.MissingPrice ? " *" : "")));
    row.firstElementChild.scope = "row";
    $("cost-models").append(row);
  }
}

function updateText(id, text) {
  if ($(id).textContent !== text) $(id).textContent = text;
}

function renderTechnical(step, job) {
  const content = DEMO_CONTENT[step.id];
  const configurations = job?.configurations || [];
  const key = JSON.stringify([step.id, configurations]);
  if (key === technicalKey) return;
  technicalKey = key;
  $("technical-title").textContent = content.title;
  $("technical-description").textContent = content.explanation;
  $("request-flow").replaceChildren();
  content.flow.forEach(text => $("request-flow").append(el("span", "flow-node", text)));
  $("technical-facts").replaceChildren();
  content.facts.forEach(([label, value]) => {
    const pair = el("div", "technical-fact");
    pair.append(el("dt", "", label), el("dd", "", value));
    $("technical-facts").append(pair);
  });
  $("configuration-snapshots").replaceChildren();
  for (const record of configurations) {
    const details = el("details", "configuration-json");
    details.append(el("summary", "", record.title), el("pre", "", record.value.text));
    if (record.value.truncated) details.append(el("p", "hint", "Configuration display truncated."));
    $("configuration-snapshots").append(details);
  }
}

function renderStages(step, job) {
  const stages = job?.stages?.length ? job.stages : context.executionStages[step.action] || [];
  $("stage-panel").hidden = !step.action || stages.length < 2 && !job?.failure;
  const key = JSON.stringify([job?.id, stages, job?.notices]);
  if (key === stagesKey) return;
  stagesKey = key;
  $("stage-results").replaceChildren();
  for (const stage of stages) {
    const row = el("tr", "stage-" + stage.status);
    const status = {pending:"Pending",running:"Running",passed:"Passed",failed:"Failed",skipped:"Skipped"}[stage.status];
    row.append(el("th", "", stage.label), el("td", "stage-state " + stage.status, status), el("td", "", duration(stage.durationMs)));
    row.firstElementChild.scope = "row";
    const retries = (job?.notices || []).filter(notice => notice.stageId === stage.id);
    const details = el("td", "stage-details", stage.error || (stage.status === "skipped" ? "Not run after the failure"
      : retries.length ? `${retries.length} backend retry notice(s). Earlier responses remain in the request history.` : ""));
    const problemExchange = stage.status === "failed" ? stage.exchangeId
      : job?.exchanges?.find(exchange => exchange.stageId === stage.id && exchange.validationError)?.id;
    if (problemExchange) {
      const inspect = el("button", "text-button", stage.status === "failed" ? "Inspect response" : "Inspect recovered error");
      inspect.type = "button";
      inspect.addEventListener("click", () => {
        selectEvidence(problemExchange);
        $("exchange-detail").scrollIntoView({block:"start",behavior:"smooth"});
      });
      details.append(inspect);
    }
    row.append(details);
    $("stage-results").append(row);
  }
}

function selection(job) {
  let value = evidenceSelections.get(job.id);
  if (!value) {
    value = {id:null,manual:false};
    evidenceSelections.set(job.id, value);
  }
  if (!value.manual) value.id = job.failure?.exchangeId || job.exchanges?.at(-1)?.id || null;
  return value;
}

function selectEvidence(identifier) {
  const job = latest(STEPS[selected].action);
  if (!job) return;
  evidenceSelections.set(job.id, {id:identifier,manual:true});
  detailKey = "";
  renderExchanges(job);
}

function makeTraceQuery(exchange) {
  if (exchange.traceId && /^[0-9a-f]{32}$/i.test(exchange.traceId)) {
    const insights = context.resources.find(item => item.id === "newInsights");
    const component = insights.href.split("#resource")[1].replace(/\/overview$/, "");
    return `OTelSpans\n| where TimeGenerated > ago(1h)\n| where _ResourceId =~ '${component}'\n| where TraceId == '${exchange.traceId}'\n| project TimeGenerated, Name, TraceId, Kind, ResultCode, DurationMs`;
  }
  return "";
}

function renderExchanges(job) {
  const exchanges = job?.exchanges || [];
  $("exchange-panel").hidden = exchanges.length === 0;
  if (!exchanges.length) return;
  if (ledgerJob !== job.id) {
    ledgerJob = job.id;
    ledgerRows.clear();
    $("request-history").replaceChildren();
    $("exchange-select").replaceChildren();
    detailKey = "";
  }
  const picked = selection(job);
  for (const record of exchanges) {
    let nodes = ledgerRows.get(record.id);
    if (!nodes) {
      const row = el("tr");
      const option = el("option");
      option.value = record.id;
      const choose = el("button", "request-select", String(record.number));
      choose.type = "button";
      choose.setAttribute("aria-label", `Inspect request ${record.number}`);
      choose.addEventListener("click", () => selectEvidence(record.id));
      const index = el("td");
      index.append(choose);
      row.append(index, el("td"), el("td"), el("td"), el("td"), el("td"));
      $("request-history").append(row);
      $("exchange-select").append(option);
      nodes = {row,option};
      ledgerRows.set(record.id, nodes);
    }
    const {row,option} = nodes;
    row.classList.toggle("selected", record.id === picked.id);
    row.querySelector("button").setAttribute("aria-pressed", String(record.id === picked.id));
    row.children[1].textContent = record.purpose || record.rpcMethod || record.requestedModel || record.target;
    row.children[2].textContent = record.httpStatus == null ? record.status === "running" ? "Pending" : "Network error"
      : `${record.httpStatus}${record.validationError ? " / tool error" : record.expected ? " expected" : record.unexpected ? " unexpected" : ""}`;
    row.children[2].className = record.unexpected || record.error || record.validationError ? "http-failed" : record.expected ? "http-expected" : record.httpStatus === 429 ? "http-limited" : "";
    row.children[3].textContent = duration(record.durationMs);
    row.children[4].textContent = record.returnedModel || "N/A";
    row.children[5].textContent = typeof record.totalTokens === "number" ? String(record.totalTokens) : "N/A";
    option.textContent = `${record.number}. ${record.purpose || record.rpcMethod || record.requestedModel || record.target}`
      + (record.httpStatus ? ` / HTTP ${record.httpStatus}` : "")
      + (record.returnedModel ? ` / ${record.returnedModel}` : "");
  }
  $("exchange-select").value = picked.id || "";
  $("history-title").textContent = `Request history (${exchanges.length} captured exchanges)`;
  $("capture-notice").textContent = job.captureLimit
    ? `Recorded ${job.captureLimit.recorded} exchanges; ${job.captureLimit.omitted} further requests exceeded the capture limit.`
    : "Credentials are redacted. Expand Full JSON for headers and complete captured bodies.";
  const exchange = exchanges.find(item => item.id === picked.id);
  $("exchange-detail").hidden = !exchange;
  if (!exchange) return;
  const key = JSON.stringify(exchange);
  if (key === detailKey) return;
  detailKey = key;
  const overview = exchangeOverview(exchange);
  updateText("input-title", overview.inputTitle);
  updateText("output-title", overview.outputTitle);
  updateText("input-content", overview.input);
  updateText("output-content", overview.output);
  $("exchange-facts").replaceChildren();
  const facts = [
    ["HTTP",exchange.httpStatus == null ? "Pending" : `${exchange.httpStatus}${exchange.expected ? " / expected" : ""}`],
    [exchange.requestedModel ? "Alias" : "Operation",exchange.requestedModel || exchange.rpcMethod || exchange.target],
    ...(exchange.returnedModel ? [["Returned model",exchange.returnedModel]] : []),
    ["Duration",duration(exchange.durationMs)],
    ...(typeof exchange.totalTokens === "number" ? [
      ["Input tokens",exchange.inputTokens ?? "Not reported"],
      ["Output tokens",exchange.outputTokens ?? "Not reported"],
      ["Total tokens",exchange.totalTokens],
    ] : []),
  ];
  for (const [label,value] of facts) {
    const pair = el("div");
    pair.append(el("dt","",label),el("dd","",String(value)));
    $("exchange-facts").append(pair);
  }
  $("exchange-target").textContent = `${exchange.method} ${exchange.target}`;
  $("exchange-duration").textContent = duration(exchange.durationMs);
  $("exchange-path").textContent = exchange.path;
  updateText("request-headers", JSON.stringify(exchange.requestHeaders, null, 2));
  updateText("response-headers", JSON.stringify(exchange.responseHeaders || {}, null, 2));
  updateText("request-body", exchange.requestBody.text === "null" ? "(no body)" : exchange.requestBody.text);
  updateText("response-body", exchange.encoding === "empty" ? "(empty notification response)"
    : exchange.responseBody?.text || (exchange.error || "Awaiting response..."));
  $("response-title").textContent = exchange.httpStatus == null ? "Response" : `Response / HTTP ${exchange.httpStatus}`;
  $("response-validation").hidden = !exchange.validationError;
  $("response-validation").textContent = exchange.validationError || "";
  $("response-body-label").textContent = exchange.encoding === "sse" ? "Decoded SSE JSON-RPC event" : "Decoded body";
  $("request-truncation").textContent = exchange.requestBody.truncated ? "Request display truncated." : "";
  $("response-truncation").textContent = exchange.responseBody?.truncated ? "Response display truncated." : "";
  $("exchange-correlation").textContent = `Correlation ID: ${exchange.correlationId || "not supplied"}\nTrace ID: ${exchange.traceId || "not supplied"}`;
  traceQuery = makeTraceQuery(exchange);
  $("trace-evidence").hidden = !exchange.correlationId && !exchange.traceId;
  $("copy-trace-query").hidden = !traceQuery;
  const scope = context.resources.find(item => item.id === "newInsights")?.href.split("#resource")[1].replace(/\/overview$/, "");
  updateText("trace-query", traceQuery.replace(scope || "<unused>", "<component-id>"));
  $("trace-copy-status").textContent = "Allow time for native trace ingestion.";
  $("trace-links").replaceChildren();
  const logs = context.resources.find(item => item.id === "workspace");
  if (logs?.href) $("trace-links").append(linkButton(logs));
}

function renderReference() {
  for (const section of DEMO_REFERENCE) {
    const panel = el("section", "reference-panel");
    panel.append(el("h2", "", section.title));
    const list = el("dl", "reference-list");
    for (const [status, title, text] of section.rows) {
      const item = el("div", "reference-item");
      const term = el("dt", "", title);
      term.append(el("span", "reference-state", status));
      item.append(term, el("dd", "", text));
      list.append(item);
    }
    const source = el("a", "reference-source", "Microsoft documentation");
    source.href = section.source;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    panel.append(list, source);
    $("reference-content").append(panel);
  }
}

function selectView(view) {
  pageView = view;
  render();
}

function renderResult(step) {
  const starting = Boolean(submitting && submitting === step.action);
  const job = starting ? null : latest(step.action);
  $("result-panel").hidden = !step.action;
  $("result-cards").replaceChildren();
  $("cost-panel").hidden = true;
  $("cost-models").replaceChildren();
  $("cost-notice").textContent = "";
  $("result-message").textContent = "";
  $("result-message").className = "result-message";
  $("execution-detail").hidden = !job;
  $("execution-output").textContent = job ? job.output + (job.error ? "\n" + job.error : "") : "";
  $("execution-status").textContent = job ? `Command: ${job.action}. Exit code: ${job.exitCode ?? "pending"}.` : "";
  if (job?.summary?.transitionRequests) $("execution-status").textContent += ` Transition requests before the final sample: ${job.summary.transitionRequests}.`;
  $("result-empty").hidden = Boolean(job) || starting;
  $("result-time").textContent = job?.completedAt
    ? new Intl.DateTimeFormat(undefined,{dateStyle:"medium",timeStyle:"short"}).format(new Date(job.completedAt)) : "";
  const status = starting ? "running" : job?.status || "not-run";
  $("step-status").hidden = !step.action;
  $("step-status").className = "status " + status;
  $("step-status").textContent = STATUS[status];
  if (starting) {
    $("result-message").textContent = "Starting...";
    return;
  }
  if (!job) return;
  if (status === "running") {
    $("result-message").textContent = job.progress ? `Running: ${job.progress}` : "Running...";
    return;
  }
  if (status === "failed") {
    $("result-message").className = "result-message failed";
    $("result-message").textContent = job.failure
      ? `${job.failure.label}: ${job.failure.error}`
      : job.error || "The command failed before a stage could start.";
    return;
  }
  const data = job.summary;
  if (step.id === "routing") {
    for (const target of data.targets) metric(target.model, target.requests, "good");
    $("result-message").textContent = data.mode === "weighted"
      ? `${data.requests} sampled requests across the three models.`
      : `Priority 0 received all ${data.requests} sampled requests.`;
  } else if (step.action === "smoke") {
    const replies = data.filter(item => item.check === "model");
    metric("Model replies", replies.length, "good");
    metric("HTTP status", replies[0]?.httpStatus, "good");
    metric("Response tokens in first reply", replies[0]?.totalTokens);
    metric("Missing or invalid credentials rejected", data.filter(item => item.httpStatus === 401 || item.httpStatus === 403).length, "good");
  } else if (step.action === "rate") {
    metric("Allowed", data.allowed, "good");
    metric("Rate limit", data.limited, "warn");
    metric("Unrestricted route", data.unlimitedAliasDuringBlock, "good");
    metric("Recovered", data.recovered, "good");
    $("result-message").textContent = `IP counter; ${data.periodSeconds}-second window. ${data.allowedBeforeBlock} requests succeeded before the 429 response.`;
  } else if (step.action === "mcp") {
    metric("Session initialized", data.initialized ? "Yes" : "No", data.initialized ? "good text" : "warn text");
    metric("Permitted tools", data.toolCount, "good");
    metric("Tool call", data.invocationSucceeded ? "Succeeded" : "Failed", data.invocationSucceeded ? "good text" : "bad text");
    metric("Model answer", data.answerPresent ? "Present" : "Missing", data.answerPresent ? "good text" : "warn text");
    metric("Returned model", data.model, "text");
    metric("Input tokens", data.inputTokens);
    metric("Output tokens", data.outputTokens);
    metric("Total tokens", data.totalTokens);
    $("result-message").textContent = `${data.tool}. Runtime key to gateway; managed identity to Foundry. Inspect the tool response for the model answer.`;
  } else if (step.action === "activity") {
    metric("Native requests", data.Requests, "good");
    metric("Last event", new Intl.DateTimeFormat(undefined,{timeStyle:"medium"}).format(new Date(data.LastSeen)), "text");
  } else if (step.action === "monitoring") {
    metric("AI Gateway requests", data.activity.Requests, "good");
    metric("Last native request", new Intl.DateTimeFormat(undefined,{timeStyle:"medium"}).format(new Date(data.activity.LastSeen)), "text");
    metric("Input tokens", data.tokens.InputTokens, "good");
    metric("Output tokens", data.tokens.OutputTokens, "good");
    renderCostMetrics(data.tokens.Costs);
    $("result-message").textContent = "Native request spans, cumulative token counters and gateway-estimated model cost. Cost is not billed spend."
      + (data.tokens.Costs?.MissingPrices ? " * The gateway flags missing prices for some token categories; estimates may be incomplete." : "");
  } else if (step.action === "telemetry") {
    const values = status === "known-gap" ? data.observed : data;
    if (values.Source !== "native-otlp") metric("Native records", values.Records);
    metric("Input tokens", values.InputTokens, status === "known-gap" ? "warn" : "good");
    metric("Output tokens", values.OutputTokens, status === "known-gap" ? "warn" : "good");
    if (status !== "known-gap") renderCostMetrics(values.Costs, true);
    $("result-message").className = "result-message " + status;
    $("result-message").textContent = status === "known-gap"
      ? "Native records report zero token usage. Model responses report non-zero usage."
      : "Native cumulative token and estimated cost counters from AI Gateway. Configuration changes can reset these counters.";
  } else if (step.action === "verify") {
    metric("Access, routing, limits, and MCP", data.coreChecksPassed ? "Passed" : "Failed", data.coreChecksPassed ? "good text" : "bad text");
    metric("Native token reporting", data.nativeTokensPassed ? "Passed" : "Failed", data.nativeTokensPassed ? "good text" : "warn text");
    $("result-message").className = "result-message " + status;
    $("result-message").textContent = status === "known-gap"
      ? "Native token reporting remains unresolved."
      : "The playground setup checks passed.";
  }
}

function render() {
  const step = STEPS[selected];
  const navigation = JSON.stringify([selected, submitting, STEPS.map(item => latest(item.action)?.status)]);
  if (navigation !== navKey) {
    navKey = navigation;
    $("steps").replaceChildren();
    STEPS.forEach((item, index) => {
    const button = el("button", "nav-step");
    button.type = "button";
    if (index === selected) button.setAttribute("aria-current", "page");
    button.append(el("span", "step-number", String(index + 1)));
    const text = el("span", "nav-text", item.label);
    const job = latest(item.action);
    const status = submitting && submitting === item.action ? "running" : job?.status;
    if (status) text.append(el("small", status, STATUS[status]));
    button.append(text);
    button.addEventListener("click", () => navigate(index));
    $("steps").append(button);
    });
  }
  $("step-count").textContent = `${selected + 1} / ${STEPS.length}`;
  $("step-title").textContent = step.title;
  $("step-description").textContent = step.description;
  $("help-step").textContent = step.label;
  $("speaker-note").textContent = step.note;
  $("caution").textContent = step.caution;
  $("orientation").hidden = step.id !== "start";
  $("resource-panel").hidden = step.id !== "resources";
  $("routing-panel").hidden = step.id !== "routing";
  $("monitoring-panel").hidden = step.id !== "monitoring";
  $("technical-panel").hidden = step.id === "monitoring";
  $("open-reset").disabled = Boolean(activeJob || submitting);
  $("model-label").textContent = step.id === "routing" ? context.routing.models.length + " models" : context.model;
  $("routing-weighted").setAttribute("aria-pressed", String(routingMode === "weighted"));
  $("routing-priority").setAttribute("aria-pressed", String(routingMode === "priority"));
  $("routing-weighted").disabled = Boolean(activeJob || submitting);
  $("routing-priority").disabled = Boolean(activeJob || submitting);
  $("routing-description").textContent = routingMode === "weighted"
    ? "Weights split requests between models at the same priority."
    : "Lower priority numbers take precedence over weights.";
  $("routing-applied").textContent = activeJob && (activeJob.action === "verify" || activeJob.action.startsWith("routing-"))
    ? "Routing is being checked. The run's configuration snapshots show the applied settings; this status refreshes after completion."
    : context.routing.mode === "priority"
    ? "Last applied: Priorities. The third model has priority 0 and receives requests while healthy. Run Weights to apply the 50 / 30 / 20 split."
    : "Last applied: Weights. All three backends have priority 0; weights split traffic 50 / 30 / 20.";
  $("routing-plan").replaceChildren();
  context.routing.models.forEach((model, index) => {
    const row = el("tr");
    row.append(
      el("th", "", model),
      el("td", "", routingMode === "weighted" || index === 2 ? "0" : "1"),
      el("td", "", context.routing.weights[index])
    );
    row.firstElementChild.scope = "row";
    $("routing-plan").append(row);
  });
  $("previous").disabled = selected === 0;
  $("next").disabled = selected === STEPS.length - 1;
  $("run-step").hidden = !step.action || pageView === "reference";
  $("run-step").disabled = Boolean(activeJob || submitting);
  $("run-step").textContent = submitting ? "Starting..." : activeJob ? "Running..." : step.id === "routing"
    ? routingMode === "weighted" ? "Apply weights + run 50" : "Apply priorities + run 10"
    : step.id === "monitoring" ? "Refresh monitoring" : latest(step.action) ? "Run again" : "Run";
  $("step-links").replaceChildren();
  for (const id of step.links) {
    let resource = context.resources.find(item => item.id === id);
    const view = GATEWAY_VIEWS[id];
    if (view) {
      const portal = context.resources.find(item => item.id === "gatewayPortal");
      if (portal?.href) resource = {id,label:view.label,href:new URL(view.path, portal.href).href};
    }
    if (resource?.href) $("step-links").append(linkButton(resource));
  }
  $("busy-banner").hidden = !activeJob || activeJob.action === step.action;
  if (activeJob) $("busy-banner").textContent = `${STEPS.find(item => item.action === activeJob.action)?.label || "Check"} is running.`;
  renderResult(step);
  if (step.id === "model") {
    if ($("exchange-panel").nextElementSibling !== $("execution-overview")) {
      $("demo-view").insertBefore($("exchange-panel"), $("execution-overview"));
    }
  } else if ($("execution-overview").nextElementSibling !== $("exchange-panel")) {
    $("demo-view").insertBefore($("execution-overview"), $("exchange-panel"));
  }
  renderTechnical(step, latest(step.action));
  renderStages(step, latest(step.action));
  renderExchanges(latest(step.action));
  $("demo-view").hidden = pageView !== "demo";
  $("reference-view").hidden = pageView !== "reference";
  $("demo-tab").setAttribute("aria-selected", String(pageView === "demo"));
  $("reference-tab").setAttribute("aria-selected", String(pageView === "reference"));
  $("demo-tab").tabIndex = pageView === "demo" ? 0 : -1;
  $("reference-tab").tabIndex = pageView === "reference" ? 0 : -1;
}

function navigate(index) {
  selected = Math.max(0, Math.min(STEPS.length - 1, index));
  history.replaceState(null, "", "?step=" + STEPS[selected].id);
  render();
  $("step-title").scrollIntoView({block:"nearest",behavior:"instant"});
}

async function runStep() {
  const action = STEPS[selected].action;
  if (!action || activeJob || submitting) return;
  submitting = action;
  render();
  try {
    const job = await api("/api/jobs", {method:"POST",body:JSON.stringify({action})});
    activeJob = job;
    results.set(action, job);
    submitting = null;
    render();
    setTimeout(() => pollJob(job), 500);
  } catch (error) {
    submitting = null;
    render();
    $("result-message").className = "result-message failed";
    $("result-message").textContent = error.message;
  }
}

async function pollJob(job) {
  try {
    const updated = await api("/api/jobs/" + job.id);
    if (job.action !== "reset-demo") results.set(job.action, updated);
    activeJob = updated.status === "running" ? updated : null;
    if (job.action === "reset-demo") {
      if (updated.status === "passed") {
        const fresh = await api("/api/context");
        resetPresentation(fresh);
        presentationChannel?.postMessage(fresh.resetAt);
        return;
      }
      resetNotice(updated.status === "failed"
        ? `Reset failed: ${updated.failure?.error || updated.error}. Existing results remain available.`
        : `Resetting playground: ${updated.progress || "restoring weighted routing"}`, updated.status === "failed");
    }
    if (!activeJob) context.routing = (await api("/api/context")).routing;
    render();
    if (activeJob) setTimeout(() => pollJob(updated), 1000);
  } catch (error) {
    if (job.action === "reset-demo") resetNotice(error.message + " Refresh to reconnect to the reset.", true);
    else {
      $("result-message").className = "result-message failed";
      $("result-message").textContent = error.message + " Refresh to reconnect.";
    }
  }
}

async function start() {
  try {
    const launch = new URLSearchParams(location.hash.slice(1)).get("launch");
    history.replaceState(null, "", location.pathname + location.search);
    const session = launch
      ? await api("/api/session", {method:"POST",body:JSON.stringify({token:launch})})
      : await api("/api/session");
    csrf = session.csrf;
    context = await api("/api/context");
    if (!["weighted", "priority"].includes(context.routing.mode)) throw new Error("The routing mode is invalid.");
    routingMode = "weighted";
    STEPS.find(step => step.id === "routing").action = "routing-" + routingMode;
    $("locked").hidden = true;
    $("workspace").hidden = false;
    $("identity-badge").textContent = "Azure CLI user verified";
    $("model-label").textContent = context.model;
    $("sku-label").textContent = context.sku;
    $("operator-name").textContent = context.operator.name;
    $("operator-subscription").textContent = context.operator.subscriptionName;
    $("operator-tenant").textContent = context.operator.tenantId;
    for (const resource of context.resources) {
      const card = el("article", "resource");
      card.append(el("small", "", resource.group), el("h2", "", LINK_LABELS[resource.id] || resource.label));
      if (resource.href) card.append(linkButton(resource, "Open"));
      else card.append(el("span", "unavailable", "Unavailable"));
      $("resource-grid").append(card);
      const detail = el("div", "help-resource");
      detail.append(resource.href ? linkButton(resource) : el("span", "", resource.label));
      detail.append(el("code", "", resource.name));
      $("help-resources").append(detail);
    }
    for (const job of context.jobs) if (job.action !== "reset-demo") results.set(job.action, job);
    renderReference();
    activeJob = context.jobs.find(job => job.status === "running") || null;
    let requested = new URLSearchParams(location.search).get("step");
    if (requested === "rehearsal") requested = "setup";
    const index = STEPS.findIndex(step => step.id === requested);
    selected = index >= 0 ? index : 0;
    render();
    if (activeJob) setTimeout(() => pollJob(activeJob), 500);
  } catch (error) {
    $("locked").hidden = false;
    $("workspace").hidden = true;
    $("identity-badge").textContent = "Authorization required";
    $("lock-error").textContent = error.message;
  }
}

function resetNotice(message, failed = false) {
  $("reset-status").hidden = false;
  $("reset-status").className = "banner" + (failed ? " reset-failed" : "");
  $("reset-status").textContent = message;
}

function resetPresentation(fresh) {
  context = fresh;
  results.clear();
  evidenceSelections.clear();
  ledgerRows.clear();
  technicalKey = stagesKey = ledgerJob = detailKey = traceQuery = navKey = "";
  activeJob = submitting = null;
  selected = 0;
  pageView = "demo";
  routingMode = "weighted";
  STEPS.find(step => step.id === "routing").action = "routing-weighted";
  for (const id of ["input-content","output-content","request-headers","response-headers","request-body","response-body","exchange-correlation","trace-query"]) $(id).textContent = "";
  for (const id of ["request-history","exchange-select","exchange-facts","trace-links"]) $(id).replaceChildren();
  document.querySelectorAll("details").forEach(detail => { detail.open = false; });
  if ($("presenter-help").open) $("presenter-help").close();
  history.replaceState(null, "", "?step=start");
  resetNotice("Playground reset. Weighted routing is restored. Azure monitoring data remains available.");
  render();
  $("main").focus({preventScroll:true});
  window.scrollTo({top:0,behavior:"instant"});
}

async function resetDemo() {
  if (activeJob || submitting) return;
  $("reset-dialog").close();
  submitting = "reset-demo";
  resetNotice("Resetting playground: restoring weighted routing...");
  render();
  window.scrollTo({top:0,behavior:"instant"});
  try {
    const job = await api("/api/jobs", {method:"POST",body:JSON.stringify({action:"reset-demo"})});
    activeJob = job;
    submitting = null;
    render();
    setTimeout(() => pollJob(job), 500);
  } catch (error) {
    submitting = null;
    resetNotice("Reset failed: " + error.message, true);
    render();
  }
}

$("open-help").addEventListener("click", () => $("presenter-help").showModal());
$("open-reset").addEventListener("click", () => $("reset-dialog").showModal());
$("cancel-reset").addEventListener("click", () => $("reset-dialog").close());
$("confirm-reset").addEventListener("click", resetDemo);
if (presentationChannel) presentationChannel.onmessage = async event => {
  if (!context || !event.data || event.data === context.resetAt) return;
  try {
    const fresh = await api("/api/context");
    if (fresh.resetAt === event.data) resetPresentation(fresh);
  } catch (error) {
    resetNotice("Could not refresh the reset state. Refresh this page: " + error.message, true);
  }
};
$("close-help").addEventListener("click", () => $("presenter-help").close());
$("presenter-help").addEventListener("close", () => {
  $("presenter-help").querySelectorAll("details").forEach(detail => { detail.open = false; });
});
$("previous").addEventListener("click", () => navigate(selected - 1));
$("next").addEventListener("click", () => navigate(selected + 1));
$("run-step").addEventListener("click", runStep);
$("demo-tab").addEventListener("click", () => selectView("demo"));
$("reference-tab").addEventListener("click", () => selectView("reference"));
for (const identifier of ["demo-tab", "reference-tab"]) {
  $(identifier).addEventListener("keydown", event => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      selectView(pageView === "demo" ? "reference" : "demo");
      $(pageView === "demo" ? "demo-tab" : "reference-tab").focus();
    }
  });
}
$("follow-latest").addEventListener("click", () => {
  const job = latest(STEPS[selected].action);
  if (job) {
    evidenceSelections.set(job.id, {id:null,manual:false});
    detailKey = "";
    renderExchanges(job);
  }
});
$("exchange-select").addEventListener("change", event => selectEvidence(event.target.value));
$("copy-trace-query").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(traceQuery);
    $("trace-copy-status").textContent = "Trace query copied with the deployed component scope. Use Workspace Logs.";
  } catch (error) {
    $("trace-copy-status").textContent = "Clipboard access failed. Inspect the query and use the private resource links for its component scope.";
  }
});
function selectRoutingMode(mode) {
  if (activeJob || submitting) return;
  routingMode = mode;
  STEPS.find(step => step.id === "routing").action = "routing-" + mode;
  render();
}
$("routing-weighted").addEventListener("click", () => selectRoutingMode("weighted"));
$("routing-priority").addEventListener("click", () => selectRoutingMode("priority"));
start();
