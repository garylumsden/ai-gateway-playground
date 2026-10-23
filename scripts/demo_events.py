"""Bounded, redacted execution evidence for the local demo interface."""

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import os
import re
import sys
import time
from urllib.parse import urlparse
import uuid

PREFIX = "@@demo:"
MAX_EXCHANGES = 180
MAX_BODY = 16384
MAX_EVIDENCE = 768000
# AI Gateway evidence can contain legacy API Management credential header names.
SENSITIVE_HEADERS = frozenset((
    "authorization", "proxy-authorization", "api-key", "ocp-apim-subscription-key",
    "cookie", "set-cookie", "mcp-session-id",
))
SENSITIVE_KEYS = frozenset((
    "authorization", "accesstoken", "refreshtoken", "token", "primarykey", "secondarykey",
    "apikey", "connectionstring", "clientsecret", "password", "secret",
    "ocpapimsubscriptionkey", "mcpsessionid", "cookie", "setcookie", "proxyauthorization",
))
STAGE_LABELS = {
    "model-access": "AI Gateway model access",
    "request-limits": "Alias request limits",
    "ai-gateway-mcp": "AI Gateway MCP tool",
    "native-activity": "Native gateway spans",
    "routing-weighted": "Weighted model routing",
    "routing-priority": "Priority model routing",
    "native-tokens": "Native token metrics",
    "reset-demo": "Reset demo",
}
ACTION_STAGES = {
    "smoke": ("model-access",),
    "rate": ("request-limits",),
    "mcp": ("ai-gateway-mcp",),
    "activity": ("native-activity",),
    "routing-weighted": ("routing-weighted",),
    "routing-priority": ("routing-priority",),
    "telemetry": ("native-tokens",),
    "monitoring": ("native-activity", "native-tokens"),
    "reset-demo": ("reset-demo",),
    "verify": ("model-access", "request-limits", "ai-gateway-mcp",
               "native-activity", "routing-priority", "routing-weighted", "native-tokens"),
}


class DemoEvents:
    def __init__(self, enabled=False, sink=None):
        self.enabled = enabled
        self.sink = sink or self.stdout
        self.hosts = {}
        self.replacements = {}
        self.secrets = set()
        self.current_stage = None
        self.last_exchange = None
        self.stages = {}
        self.count = 0
        self.size = 0
        self.omitted = 0
        self.next_purpose = None
        self.next_expected = ()

    @staticmethod
    def stdout(event):
        print(PREFIX + json.dumps(event, ensure_ascii=True), flush=True)

    def configure(self, config, state):
        for value, label in (
            (config["subscriptionId"], "<subscription-id>"),
            (state.get("foundryName"), "<foundry-account>"),
            (state.get("logWorkspaceName"), "<log-workspace>"),
            (state.get("hostname"), "<ai-gateway>"),
            (state.get("gatewayName"), "<gateway-resource>"),
            (state.get("applicationInsightsId", "").rsplit("/", 1)[-1], "<app-insights>"),
        ):
            if value:
                self.replacements[value] = label
        for identifier in re.findall(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", json.dumps(state),
        ):
            self.replacements[identifier] = "<resource-id>"
        if state.get("hostname"):
            self.observe_host(state["hostname"], "AI Gateway")
        self.observe_host("api.loganalytics.azure.com", "Workspace Logs")

    def observe_host(self, hostname, label):
        if hostname:
            self.hosts[hostname.lower()] = label

    def identifier(self, value, label):
        if value:
            self.replacements[value] = label

    def text(self, value):
        result = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            result = result.replace(secret, "<redacted>")
        for original in sorted(self.replacements, key=len, reverse=True):
            result = result.replace(original, self.replacements[original])
        result = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.~+/-]{16,}", "Bearer <redacted>", result)
        result = re.sub(r"(?i)InstrumentationKey=[^\s\";]+", "InstrumentationKey=<redacted>", result)
        result = re.sub(r"(?i)[A-Z]:\\Users\\[^\\\s]+", r"C:\\Users\\<operator>", result)
        result = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "<account>", result)
        return result

    def clean(self, value):
        if isinstance(value, dict):
            return {
                key: (
                    "<redacted>" if re.sub(r"[-_]", "", key).lower() in SENSITIVE_KEYS
                    else {header: "<redacted>" for header in child} if key.lower() == "headers" and isinstance(child, dict)
                    else self.clean(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self.clean(child) for child in value]
        return self.text(value) if isinstance(value, str) else value

    def headers(self, values):
        result = {}
        for key, value in values.items():
            if key.lower() in SENSITIVE_HEADERS:
                if isinstance(value, str) and len(value) >= 8:
                    self.secrets.add(value)
                    if value.lower().startswith("bearer "):
                        self.secrets.add(value[7:])
                result[key] = "<redacted>"
            else:
                result[key] = self.text(value)
        return result

    def body(self, value):
        clean = self.clean(value)
        text = json.dumps(clean, indent=2, ensure_ascii=True) if not isinstance(clean, str) else clean
        remaining = max(0, min(MAX_BODY, MAX_EVIDENCE - self.size))
        truncated = len(text) > remaining
        text = text[:remaining]
        self.size += len(text)
        return {"text": text, "format": "text" if isinstance(clean, str) else "json", "truncated": truncated}

    def emit(self, kind, **values):
        if self.enabled:
            self.sink({"type": kind, "at": datetime.now(timezone.utc).isoformat(), **self.clean(values)})

    def plan(self, action, routing=True):
        identifiers = ACTION_STAGES.get(action, ())
        if not routing:
            identifiers = tuple(item for item in identifiers if not item.startswith("routing-"))
        self.stages = {identifier: "pending" for identifier in identifiers}
        self.emit("plan", stages=[{"id": item, "label": STAGE_LABELS[item], "status": "pending"} for item in identifiers])

    @contextmanager
    def stage(self, identifier, label=None):
        if not self.enabled:
            yield
            return
        previous = self.current_stage
        self.current_stage = identifier
        self.last_exchange = None
        self.stages[identifier] = "running"
        started = time.monotonic()
        self.emit("stage", id=identifier, label=label or STAGE_LABELS[identifier], status="running")
        try:
            yield
        except Exception as error:
            self.stages[identifier] = "failed"
            self.emit("stage", id=identifier, label=label or STAGE_LABELS[identifier], status="failed",
                      error=self.text(error), durationMs=round((time.monotonic() - started) * 1000),
                      exchangeId=self.last_exchange)
            raise
        else:
            self.stages[identifier] = "passed"
            self.emit("stage", id=identifier, label=label or STAGE_LABELS[identifier], status="passed",
                      durationMs=round((time.monotonic() - started) * 1000))
        finally:
            self.current_stage = previous

    def finish(self):
        for identifier, status in self.stages.items():
            if status == "pending":
                self.emit("stage", id=identifier, label=STAGE_LABELS[identifier], status="skipped")
        if self.omitted:
            self.emit("capture-limit", recorded=self.count, omitted=self.omitted)

    def configuration(self, title, value):
        if self.enabled:
            self.emit("configuration", stageId=self.current_stage, title=title, value=self.body(value))

    def notice(self, message, **values):
        self.emit("notice", stageId=self.current_stage, message=message, **values)

    def invalid_exchange(self, message):
        if self.last_exchange:
            self.emit("exchange-invalid", id=self.last_exchange, message=message)

    def expect(self, purpose, statuses=()):
        self.next_purpose = purpose
        self.next_expected = statuses

    def begin(self, method, url, headers, body):
        self.headers(headers)
        parsed = urlparse(url)
        purpose, expected = self.next_purpose, self.next_expected
        self.next_purpose, self.next_expected = None, ()
        if not self.enabled or parsed.hostname not in self.hosts:
            return None
        if self.count >= MAX_EXCHANGES:
            self.omitted += 1
            self.last_exchange = None
            return None
        self.count += 1
        identifier = uuid.uuid4().hex
        self.last_exchange = identifier
        record = {
            "id": identifier, "stageId": self.current_stage, "number": self.count,
            "target": self.hosts[parsed.hostname], "method": method,
            "path": parsed.path + ("?" + self.text(parsed.query) if parsed.query else ""),
            "requestHeaders": self.headers(headers), "requestBody": self.body(body),
            "correlationId": next((value for key, value in headers.items() if key.lower() == "x-correlation-id"), None),
            "traceId": next((
                match.group(1)
                for key, value in headers.items()
                if key.lower() == "traceparent"
                and (match := re.fullmatch(r"00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}", value, re.IGNORECASE))
            ), None),
            "status": "running",
            "purpose": purpose,
            "expectedStatuses": list(expected),
            "requestedModel": body.get("model") if isinstance(body, dict) else None,
            "rpcMethod": body.get("method") if isinstance(body, dict) else None,
        }
        self.emit("exchange-start", exchange=record)
        return identifier, time.monotonic(), expected

    def end(self, handle, status, headers, body, *, encoding="json", error=None):
        if handle is None:
            return
        identifier, started, expected = handle
        response_headers = self.headers(headers)
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        self.emit(
            "exchange-end", id=identifier, httpStatus=status, responseHeaders=response_headers,
            responseBody=self.body(body), encoding=encoding, error=self.text(error) if error else None,
            durationMs=round((time.monotonic() - started) * 1000),
            returnedModel=body.get("model") if isinstance(body, dict) else None,
            inputTokens=usage.get("input_tokens", usage.get("prompt_tokens")),
            outputTokens=usage.get("output_tokens", usage.get("completion_tokens")),
            totalTokens=usage.get("total_tokens"),
            expected=bool(expected and status in expected),
            unexpected=bool(expected and status not in expected),
        )


EVENTS = DemoEvents(os.environ.get("DEMO_EVENTS") == "1")


def apply_event(job, event):
    kind = event.get("type")
    if kind == "plan":
        job["stages"] = event["stages"]
    elif kind == "stage":
        stages = job.setdefault("stages", [])
        current = next((item for item in stages if item["id"] == event["id"]), None)
        if current is None:
            current = {"id": event["id"]}
            stages.append(current)
        current.update({key: value for key, value in event.items() if key != "type"})
        if event["status"] == "running":
            current["startedAt"] = event["at"]
            job["progress"] = event["label"]
        elif event["status"] == "failed":
            current["completedAt"] = event["at"]
            job["failure"] = dict(current)
        elif event["status"] == "passed":
            current["completedAt"] = event["at"]
    elif kind == "configuration":
        records = job.setdefault("configurations", [])
        record = {key: value for key, value in event.items() if key != "type"}
        prior = next((item for item in records if item["stageId"] == record["stageId"] and item["title"] == record["title"]), None)
        if prior:
            prior.update(record)
        else:
            records.append(record)
    elif kind == "exchange-start":
        records = job.setdefault("exchanges", [])
        if len(records) >= MAX_EXCHANGES:
            raise ValueError("The execution evidence exceeded its request limit.")
        records.append(event["exchange"])
    elif kind == "exchange-end":
        current = next((item for item in job.get("exchanges", []) if item["id"] == event["id"]), None)
        if current is None:
            raise ValueError("A response has no matching request.")
        current.update({key: value for key, value in event.items() if key != "type"})
        current["status"] = "failed" if event.get("error") or event.get("unexpected") else "complete"
    elif kind == "exchange-invalid":
        current = next((item for item in job.get("exchanges", []) if item["id"] == event["id"]), None)
        if current is not None:
            current.update({"validationError": event["message"], "status": "failed"})
    elif kind == "notice":
        notices = job.setdefault("notices", [])
        notices.append({key: value for key, value in event.items() if key != "type"})
        job["progress"] = event["message"]
    elif kind == "capture-limit":
        job["captureLimit"] = {"recorded": event["recorded"], "omitted": event["omitted"]}
    else:
        raise ValueError("Unknown execution evidence event.")


def recorded_stage(identifier):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with EVENTS.stage(identifier):
                return function(*args, **kwargs)
        return call
    return decorate
