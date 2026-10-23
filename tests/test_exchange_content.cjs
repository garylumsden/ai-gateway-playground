const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const scope = {URLSearchParams};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "demo_content.js"), "utf8"), scope);
const body = value => ({text:JSON.stringify(value),format:"json",truncated:false});
const exchange = (request, response, extra = {}) => ({
  requestBody:body(request),responseBody:body(response),httpStatus:200,...extra,
});

test("shows the user prompt and actual chat answer without JSON metadata", () => {
  const view = scope.exchangeOverview(exchange({
    model:"example-alias",
    messages:[{role:"system",content:"Hidden setup."},{role:"user",content:"Review three missing readings."}],
  }, {
    model:"example-model",choices:[{message:{content:"Check the collection time."}}],
    usage:{total_tokens:32},
  }));
  assert.equal(view.inputTitle,"Input prompt");
  assert.equal(view.input,"Review three missing readings.");
  assert.equal(view.output,"Check the collection time.");
  assert.ok(!view.input.includes("Hidden setup"));
  assert.ok(!view.output.includes("total_tokens"));
});

test("supports Responses API text and message input", () => {
  for (const input of ["Review the source.",[{role:"user",content:[{type:"input_text",text:"Review the source."}]}]]) {
    const view = scope.exchangeOverview(exchange({input}, {
      output:[{type:"message",content:[{type:"output_text",text:"The source has no date."}]}],
    }));
    assert.equal(view.input,"Review the source.");
    assert.equal(view.output,"The source has no date.");
  }
});

test("labels expected authentication rejections without fabricating an answer", () => {
  const view = scope.exchangeOverview(exchange({messages:[{role:"user",content:"Review the source."}]}, {
    error:{code:"Unauthorized",message:"No access key supplied."},
  },{httpStatus:401,expected:true}));
  assert.equal(view.outputTitle,"Expected rejection");
  assert.equal(view.output,"No access key supplied.");
});

test("keeps a semantic MCP failure visible inside HTTP 200", () => {
  const view = scope.exchangeOverview(exchange({
    method:"tools/call",params:{name:"model_summarize_text",arguments:{model:"primary-deployment",max_completion_tokens:96}},
  },{result:{isError:false,content:[{type:"text",text:"<html>Server error</html>"}]}},{
    validationError:"The model tool returned an unreadable response.",
  }));
  assert.match(view.input,/Tool: model_summarize_text/);
  assert.match(view.input,/model: primary-deployment/);
  assert.match(view.input,/max_completion_tokens: 96/);
  assert.equal(view.output,"The model tool returned an unreadable response.");
  assert.equal(view.outputTitle,"Request outcome");
});

test("shows model answers from structured and text MCP results without JSON metadata", () => {
  const answer = {model:"primary-model",choices:[{message:{content:"Assign the two owners and set the due date."}}],usage:{total_tokens:40}};
  const prompt = "Summarize: A project has three open tasks. Two need owners and one needs a due date. State the next action in one sentence.";
  for (const result of [{structuredContent:answer},{content:[{type:"text",text:JSON.stringify(answer)}]}]) {
    const view = scope.exchangeOverview(exchange({
      method:"tools/call",params:{name:"model_summarize_text",arguments:{
        model:"primary-deployment",messages:[{role:"user",content:prompt}],max_completion_tokens:96,reasoning_effort:"none",
      }},
    },{jsonrpc:"2.0",id:3,result}));
    assert.match(view.input,/model: primary-deployment/);
    assert.ok(view.input.includes(prompt));
    assert.equal(view.output,"Assign the two owners and set the due date.");
    assert.ok(!view.output.includes("total_tokens"));
  }
});

test("preserves arbitrary tool text and structured data", () => {
  for (const [result,expected] of [
    [{content:[{type:"text",text:"Three tasks need review."}]},"Three tasks need review."],
    [{structuredContent:{tasks:3}},'{\n  "tasks": 3\n}'],
    [{structuredContent:"Review the tasks."},"Review the tasks."],
    [{content:[{type:"text",text:"{invalid JSON"}]},"{invalid JSON"],
    [{structuredContent:0},"0"],
  ]) {
    const view = scope.exchangeOverview(exchange({method:"tools/call",params:{name:"task_status"}},{result}));
    assert.equal(view.output,expected);
  }
});

test("shows MCP tool errors even when the HTTP request succeeds", () => {
  for (const result of [
    {isError:true,content:[{type:"text",text:"Model access denied."}]},
    {isError:true,structuredContent:{error:{message:"Model access denied."}}},
  ]) {
    const view = scope.exchangeOverview(exchange({method:"tools/call",params:{name:"model_summarize_text"}},{result}));
    assert.equal(view.outputTitle,"Request outcome");
    assert.equal(view.output,"Model access denied.");
  }
});

test("does not invent a model answer when a tool returns empty content", () => {
  const view = scope.exchangeOverview(exchange({method:"tools/call",params:{name:"model_summarize_text"}},{result:{content:[]}}));
  assert.equal(view.output,"The tool returned no readable content. Inspect Full JSON.");
});

test("shows pending, malformed, and truncated body notices", () => {
  const pending = scope.exchangeOverview({requestBody:body({messages:[{role:"user",content:"Review."}]})});
  assert.equal(pending.output,"Awaiting response...");
  for (const requestBody of [
    {text:"{",format:"json",truncated:false},
    {text:"{",format:"json",truncated:true},
  ]) {
    const view = scope.exchangeOverview({requestBody,responseBody:body({})});
    assert.match(view.input,/Open Full JSON/);
  }
});

test("shows actual monitoring result columns", () => {
  const view = scope.exchangeOverview(exchange({query:"OTelSpans | count"}, {
    tables:[{columns:[{name:"Requests"},{name:"LastSeen"}],rows:[[8,"2030-01-15T18:00:00Z"]]}],
  }));
  assert.equal(view.inputTitle,"Monitoring query");
  assert.match(view.output,/Requests: 8/);
  assert.match(view.output,/LastSeen: 2030-01-15T18:00:00Z/);
});

test("shows the actual PromQL query and native metric values", () => {
  const view = scope.exchangeOverview(exchange(null, {
    data:{result:[{metric:{"gen_ai.request.model":"example-model","gen_ai.token.type":"input_tokens"},value:[1,"124"]}]},
  },{target:"Native token metrics",path:"/api/v1/query?query=last_over_time%28example%5B1h%5D%29"}));
  assert.equal(view.input,"last_over_time(example[1h])");
  assert.equal(view.output,"example-model / input_tokens / 124");
});

test("shows gateway cost metric samples with their recorded currency", () => {
  const view = scope.exchangeOverview(exchange(null, {
    data:{result:[{metric:{"gen_ai.request.model":"example-alias","gen_ai.token.type":"total_tokens","azure.ai_gateway.currency":"usd"},value:[1,"0.00125"]}]},
  },{target:"AI Gateway metrics",path:"/api/v1/query?query=azure.ai_gateway.client.token.cost"}));
  assert.equal(view.outputTitle,"Native estimated cost samples");
  assert.equal(view.output,"example-alias / total_tokens / 0.00125 / USD");
});
