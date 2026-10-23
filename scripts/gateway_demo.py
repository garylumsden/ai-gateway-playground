"""Deploy and verify an AI Gateway without storing runtime credentials."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse
import uuid
from demo_events import EVENTS, recorded_stage

ROOT = Path(__file__).resolve().parents[1]
PROVISION_API = "2026-09-01-preview"
ASSET_API = "2026-05-01"
MODEL_ALIAS = "playground-model"
LIMITED_ALIAS = "playground-limited"
ROUTING_ALIAS = "playground-routing"
ROUTING_WEIGHTS = (50, 30, 20)
PORTAL_ASSET_API = "2025-09-01-preview"
MONITOR_PUBLISHER_ROLE = "3913510d-42f4-4e42-8a64-420c390055eb"
NATIVE_TOKEN_METRIC = "azure.ai_gateway.client.token.usage"
NATIVE_COST_METRIC = "azure.ai_gateway.client.token.cost"
MODEL_PROMPTS = (
    "Review this support-ticket dataset: 24 records; three missing creation times; "
    "one ticket without a priority. Write a short handover with "
    "Observations, Evidence checks, and Next action. Only the aggregate facts above are available. "
    "Describe checks to perform, not completed checks. Do not invent causes or resolutions. "
    "Use plain text and at most 90 words.",
    "Design an access plan for a software project team. "
    "The team has 12 developers, two service accounts, and a nightly summarisation job. "
    "State one control each for client authentication, "
    "model access, usage attribution, and safe logging. Treat these as design requirements, not "
    "verified product capabilities. Use plain text and at most 90 words.",
    "Review three sources for a project report. Record A has a source and date. "
    "Record B has no provenance (source history). Record C is six years old. Identify the evidence gaps and two "
    "checks before using the records. Do not invent measurements or conclusions. "
    "Use plain text and at most 80 words.",
)
ROUTING_PROMPT = (
    "A support ticket has no creation time and no priority. "
    "Give a project team one data-quality check before using it. Use at most 20 words."
)
LIMIT_PROMPT = (
    "A project report cites an undated source. "
    "State one evidence check before using it. Use one sentence of at most 25 words."
)


def native_token_totals(payload: dict) -> dict:
    data = payload.get("data", {})
    if payload.get("status") != "success" or not isinstance(data, dict) or data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
        raise DemoError("The native token query did not return a metric vector.")
    result = {"Records": len(data["result"]), "InputTokens": 0.0, "OutputTokens": 0.0, "Source": "native-otlp"}
    for series in data["result"]:
        point = series.get("value")
        if not isinstance(point, list) or len(point) != 2 or isinstance(point[1], bool):
            raise DemoError("A native token metric series has no valid sample.")
        try:
            value = float(point[1])
        except (TypeError, ValueError) as error:
            raise DemoError("A native token metric sample is not numeric.") from error
        if not math.isfinite(value) or value < 0:
            raise DemoError("A native token metric sample is invalid.")
        token_type = str(series.get("metric", {}).get("gen_ai.token.type", "")).replace("_", "").lower()
        if token_type in ("input", "prompt", "inputtokens", "prompttokens", "inputtokencount", "prompttokencount"):
            result["InputTokens"] += value
        elif token_type in ("output", "completion", "outputtokens", "completiontokens", "outputtokencount", "completiontokencount"):
            result["OutputTokens"] += value
    return result


def native_cost_totals(payload: dict) -> dict:
    data = payload.get("data", {})
    if payload.get("status") != "success" or not isinstance(data, dict) or data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
        raise DemoError("The native cost query did not return a metric vector.")
    groups = {}
    for series in data["result"]:
        if not isinstance(series, dict):
            raise DemoError("A native cost metric series is invalid.")
        labels = series.get("metric", {})
        if not isinstance(labels, dict):
            raise DemoError("A native cost metric series has invalid labels.")
        token_type = labels.get("gen_ai.token.type")
        if not token_type:
            continue
        point = series.get("value")
        if not isinstance(point, list) or len(point) != 2 or isinstance(point[1], bool):
            raise DemoError("A native cost metric series has no valid sample.")
        try:
            value = float(point[1])
        except (TypeError, ValueError) as error:
            raise DemoError("A native cost metric sample is not numeric.") from error
        if not math.isfinite(value) or value < 0:
            raise DemoError("A native cost metric sample is invalid.")
        currency = labels.get("azure.ai_gateway.currency", "")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise DemoError("A native cost metric sample has no valid currency label.")
        key = json.dumps({
            name: content for name, content in labels.items()
            if name not in ("__name__", "gen_ai.token.type", "azure.ai_gateway.price_missing")
        }, sort_keys=True)
        group = groups.setdefault(key, {
            "Alias": labels.get("gen_ai.request.model") or "Not reported",
            "Model": labels.get("gen_ai.response.model") or labels.get("azure.ai_gateway.resolved_model.name") or "Not reported",
            "Currency": currency.upper(), "InputCost": None, "OutputCost": None, "TotalCost": None,
            "MissingPrice": False,
        })
        group["MissingPrice"] |= str(labels.get("azure.ai_gateway.price_missing", "")).lower() == "true"
        normalized = str(token_type).replace("_", "").lower()
        field = (
            "InputCost" if normalized in ("input", "prompt", "inputtokens", "prompttokens", "inputtokencount", "prompttokencount")
            else "OutputCost" if normalized in ("output", "completion", "outputtokens", "completiontokens", "outputtokencount", "completiontokencount")
            else "TotalCost" if normalized in ("total", "totaltokens", "totaltokencount")
            else None
        )
        if field:
            if group[field] is not None:
                raise DemoError("Native cost metrics contain duplicate base cost categories.")
            group[field] = value

    models = {}
    for group in groups.values():
        if group["TotalCost"] is None and (group["InputCost"] is not None or group["OutputCost"] is not None):
            group["TotalCost"] = (group["InputCost"] or 0) + (group["OutputCost"] or 0)
        if group["TotalCost"] is None and not group["MissingPrice"]:
            continue
        key = (group["Alias"], group["Model"], group["Currency"])
        row = models.setdefault(key, {
            "Alias": key[0], "Model": key[1], "Currency": key[2],
            "InputCost": None, "OutputCost": None, "TotalCost": None, "MissingPrice": False,
        })
        for field in ("InputCost", "OutputCost", "TotalCost"):
            if group[field] is not None:
                row[field] = (row[field] or 0) + group[field]
        row["MissingPrice"] |= group["MissingPrice"]

    currencies = {}
    for row in models.values():
        summary = currencies.setdefault(row["Currency"], {
            "Currency": row["Currency"], "TotalCost": None, "Complete": True,
        })
        if row["TotalCost"] is not None:
            summary["TotalCost"] = (summary["TotalCost"] or 0) + row["TotalCost"]
        if row["MissingPrice"] or row["TotalCost"] is None:
            summary["Complete"] = False
    totals = sorted(currencies.values(), key=lambda item: item["Currency"])
    available = any(item["TotalCost"] is not None for item in totals)
    return {
        "Available": available, "Source": "native-otlp", "Metric": NATIVE_COST_METRIC,
        "Records": len(data["result"]), "Lookback": "1h", "Aggregation": "latest-cumulative",
        "Currencies": totals, "Models": sorted(models.values(), key=lambda item: (item["Currency"], item["Alias"], item["Model"])),
        "MissingPrices": any(row["MissingPrice"] for row in models.values()),
        "Reason": "" if available else "No reported native model cost is available in the last hour.",
    }


class DemoError(Exception):
    """An explicit failure with no credential or response-body disclosure."""


class NativeTokenGap(DemoError):
    """Native records arrived, but the gateway reported zero model token usage."""

def tool_model_result(result: dict) -> dict:
    if result.get("isError"):
        raise DemoError("The model tool returned an error.")
    if isinstance(result.get("structuredContent"), dict):
        payload = result["structuredContent"]
    else:
        texts = [
            part["text"] for part in result.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        try:
            payload = json.loads(texts[0].lstrip("\ufeff")) if texts else {}
        except json.JSONDecodeError as error:
            raise DemoError("The model tool did not return valid JSON.") from error
    return Demo.model_result(200, payload)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require_runtime_url(url: str, hostname: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.hostname != hostname
        or parsed.username or parsed.password or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise DemoError("The runtime URL must use the gateway's own HTTPS hostname.")
    return url


def runtime_endpoint(
    resource: dict, operation: str, hostname: str, documented_prefix: str | None = None,
) -> str:
    for endpoint in resource.get("properties", {}).get("gatewayEndpoints", []):
        url = endpoint.get("url", "")
        if urlparse(url).path.rstrip("/").endswith("/" + operation):
            return require_runtime_url(url, hostname)
    if documented_prefix is not None:
        for endpoint in resource.get("properties", {}).get("supportedEndpoints", []):
            if isinstance(endpoint, str) and endpoint.endswith("/" + operation):
                if not endpoint.startswith("/") or ".." in endpoint:
                    raise DemoError("The service returned an invalid relative endpoint.")
                return require_runtime_url(
                    f"https://{hostname}{documented_prefix}{endpoint}", hostname,
                )
    raise DemoError(f"The gateway did not return an invocation endpoint for {operation}.")


def secret_values(value: object) -> list[str]:
    found = []
    if isinstance(value, dict):
        for name, child in value.items():
            if name.lower() in ("primarykey", "secondarykey", "connectionstring", "accesstoken", "apikey"):
                if isinstance(child, str):
                    found.append(child)
            elif name.lower() == "headers" and isinstance(child, dict):
                for header in child.values():
                    if isinstance(header, list):
                        found.extend(item for item in header if isinstance(item, str))
                    elif isinstance(header, str):
                        found.append(header)
            elif name.lower() == "values" and isinstance(child, list):
                found.extend(item for item in child if isinstance(item, str))
            else:
                found.extend(secret_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(secret_values(child))
    return found


def az(arguments: list[str]) -> object:
    executable = shutil.which("az")
    if not executable:
        raise DemoError("Azure CLI is required.")
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors", "--output", "json"],
        capture_output=True, text=True, encoding="utf-8-sig", env=environment, cwd=ROOT,
        check=False,
    )
    if result.returncode:
        # CLI errors can contain resource identifiers; retain them only in ignored local state.
        local_error = ROOT / ".azure" / "last-cli-error.txt"
        local_error.parent.mkdir(exist_ok=True)
        local_error.write_text(result.stderr, encoding="utf-8")
        raise DemoError(f"Azure CLI failed. Inspect {local_error.relative_to(ROOT)}.")
    try:
        return json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise DemoError("Azure CLI did not return valid JSON.") from error


def request(
    method: str, url: str, headers: dict[str, str], body: object = None,
) -> tuple[int, dict[str, str], object]:
    if not url.startswith("https://"):
        raise DemoError("HTTPS is required.")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **headers},
    )
    evidence = EVENTS.begin(method, url, req.headers, body)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as response:
            content = response.read()
            text = content.decode("utf-8-sig")
            try:
                parsed = json.loads(text) if content else None
            except json.JSONDecodeError:
                EVENTS.end(evidence, response.status, dict(response.headers.items()), text, encoding="text")
                raise
            EVENTS.end(evidence, response.status, dict(response.headers.items()), parsed)
            return response.status, dict(response.headers.items()), parsed
    except urllib.error.HTTPError as error:
        with error:
            content = error.read()
            try:
                parsed = json.loads(content.decode("utf-8-sig")) if content else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                EVENTS.end(evidence, error.code, dict(error.headers.items()), content.decode("utf-8", errors="replace"), encoding="text")
                parsed = {"error": {"code": "NonJsonError"}}
            else:
                EVENTS.end(evidence, error.code, dict(error.headers.items()), parsed)
            return error.code, dict(error.headers.items()), parsed
    except (urllib.error.URLError, TimeoutError) as error:
        EVENTS.end(evidence, None, {}, None, error="HTTPS request failed or timed out.")
        raise DemoError("The HTTPS request failed or timed out.") from error


class Demo:
    def __init__(self, config_path: Path):
        try:
            self.config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise DemoError("Provide a valid ignored .azure/config.json.") from error
        self.subscription = str(uuid.UUID(self.config["subscriptionId"]))
        self.state_path = ROOT / ".azure" / "state.json"
        self.tokens: dict[str, str] = {}
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        EVENTS.configure(self.config, self.state)
        for name in (
            "location", "resourceGroupName", "deploymentName", "model",
        ):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.config[name]):
                raise DemoError(f"Invalid configuration field: {name}.")
        deployments = self.config.get("modelDeployments")
        if not isinstance(deployments, list) or len(deployments) != 3:
            raise DemoError("Configure exactly three modelDeployments.")
        names = set()
        models = set()
        for deployment in deployments:
            if (
                not isinstance(deployment, dict)
                or set(deployment) != {"name", "model", "version", "sku", "capacity"}
                or any(not isinstance(deployment[field], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", deployment[field])
                       for field in ("name", "model", "version", "sku"))
                or type(deployment["capacity"]) is not int or deployment["capacity"] < 1
            ):
                raise DemoError("A model deployment definition is invalid.")
            names.add(deployment["name"])
            models.add(deployment["model"])
        if len(names) != 3 or len(models) != 3:
            raise DemoError("Model deployment names and model names must be distinct for routing experiments.")
        routing = self.config.get("routingModels")
        if (
            self.config["model"] not in names
            or not isinstance(routing, list) or len(routing) != 3
            or any(not isinstance(name, str) for name in routing)
            or set(routing) != names
        ):
            raise DemoError("model and routingModels must reference the declared model deployments.")
        self.prefix = f"/subscriptions/{self.subscription}"
        self.deployment_path = (
            f"{self.prefix}/providers/Microsoft.Resources/deployments/"
            f"{self.config['deploymentName']}"
        )

    def save(self) -> None:
        self.state_path.parent.mkdir(exist_ok=True)
        temporary = self.state_path.with_name(f"state-{os.getpid()}.json.tmp")
        try:
            temporary.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def token(self, audience: str = "https://management.azure.com/") -> str:
        if audience not in self.tokens:
            result = az([
                "account", "get-access-token", "--resource", audience,
                "--subscription", self.subscription,
            ])
            if not isinstance(result, dict) or not result.get("accessToken"):
                raise DemoError("A non-interactive Azure access token is required.")
            EVENTS.identifier(result.get("tenant"), "<directory-id>")
            self.tokens[audience] = result["accessToken"]
        return self.tokens[audience]

    def arm(
        self, path: str, method: str = "GET", body: object = None,
        version: str = ASSET_API, allowed: tuple[int, ...] = (200, 201, 202, 204),
    ) -> object:
        status, _, payload = request(
            method, f"https://management.azure.com{path}?api-version={version}",
            {"Authorization": f"Bearer {self.token()}"}, body,
        )
        if status not in allowed:
            code = payload.get("error", {}).get("code", "Unknown") if isinstance(payload, dict) else "Unknown"
            diagnostic = json.dumps({"status": status, "path": path, "response": payload}, indent=2)
            for secret in [*self.tokens.values(), *secret_values(body)]:
                if secret:
                    diagnostic = diagnostic.replace(secret, "[REDACTED]")
            error_file = ROOT / ".azure" / "last-arm-error.json"
            error_file.parent.mkdir(exist_ok=True)
            error_file.write_text(diagnostic, encoding="utf-8")
            raise DemoError(
                f"ARM {method} {path.rsplit('/', 1)[-1]} failed: HTTP {status}, code {code}. "
                "Inspect .azure/last-arm-error.json."
            )
        return payload

    def parameters(self) -> dict:
        return {
            key: {"value": self.config[key]}
            for key in ("location", "resourceGroupName", "modelDeployments")
        }

    def template(self) -> dict:
        result = az(["bicep", "build", "--file", str(ROOT / "infra" / "main.bicep"), "--stdout"])
        if not isinstance(result, dict):
            raise DemoError("Bicep did not return a deployment template.")
        return result

    def fingerprint(self) -> str:
        digest = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode())
        for path in sorted((ROOT / "infra").rglob("*.bicep")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def preflight(self) -> None:
        self.state.pop("validation", None)
        self.state.pop("preview", None)
        self.save()
        body = {
            "location": self.config["location"],
            "properties": {
                "mode": "Incremental", "template": self.template(),
                "parameters": self.parameters(),
            },
        }
        validation = self.arm(
            self.deployment_path + "/validate", "POST", body, version="2025-04-01",
        )
        if isinstance(validation, dict) and validation.get("error"):
            raise DemoError("Azure deployment validation returned an error.")
        self.state["validation"] = "passed"
        self.state["validatedFingerprint"] = self.fingerprint()
        self.save()
        print("Azure deployment validation passed.")

    def preview(self) -> None:
        self.state.pop("preview", None)
        self.save()
        if self.state.get("validatedFingerprint") != self.fingerprint():
            raise DemoError("Infrastructure or configuration changed. Run preflight again.")
        parameter_file = ROOT / ".azure" / "parameters.json"
        parameter_file.parent.mkdir(exist_ok=True)
        parameter_file.write_text(json.dumps(self.parameters()), encoding="utf-8")
        result = az([
            "deployment", "sub", "what-if",
            "--subscription", self.subscription,
            "--location", self.config["location"],
            "--name", self.config["deploymentName"],
            "--template-file", str(ROOT / "infra" / "main.bicep"),
            "--parameters", f"@{parameter_file}",
            "--validation-level", "Provider",
            "--no-pretty-print",
        ])
        local_preview = ROOT / ".azure" / "what-if.json"
        local_preview.write_text(json.dumps(result, indent=2), encoding="utf-8")
        changes = result.get("changes", [])
        owned_prefix = f"{self.prefix}/resourceGroups/{self.config['resourceGroupName']}/providers/"
        for change in changes:
            resource_id = change["resourceId"]
            owned = resource_id.lower().startswith(owned_prefix.lower())
            if change["changeType"] == "Delete" or (change["changeType"] == "Modify" and not owned):
                raise DemoError("What-if changes a resource outside the playground or deletes data. Inspect .azure/what-if.json.")
        self.state["preview"] = "passed"
        self.save()
        print(json.dumps({
            "whatIf": "passed",
            "changes": [{"type": item["changeType"], "resourceType": item["resourceId"].split("/providers/")[-1].rsplit("/", 1)[0]} for item in changes],
        }, indent=2))

    def deploy(self) -> None:
        if (
            self.state.get("validation") != "passed" or self.state.get("preview") != "passed"
            or self.state.get("validatedFingerprint") != self.fingerprint()
        ):
            raise DemoError("Run preflight and preview before deployment.")
        body = {
            "location": self.config["location"],
            "properties": {
                "mode": "Incremental", "template": self.template(),
                "parameters": self.parameters(),
            },
        }
        self.arm(self.deployment_path, "PUT", body, version="2025-04-01")
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            result = self.arm(self.deployment_path, version="2025-04-01")
            properties = result["properties"]
            status = properties.get("provisioningState")
            if status == "Succeeded":
                self.state.update({
                    key: value["value"] for key, value in properties["outputs"].items()
                })
                self.state["deployed"] = True
                self.save()
                print("AI Gateway infrastructure deployed.")
                return
            if status in ("Failed", "Canceled"):
                raise DemoError(f"Deployment {status}. Inspect Azure deployment operations.")
            time.sleep(10)
        raise DemoError("Deployment did not complete within 20 minutes.")

    @property
    def gateway_id(self) -> str:
        value = self.state.get("gatewayId")
        if not value:
            raise DemoError("Deploy the gateway first.")
        return value

    def inspect(self) -> None:
        gateway = self.arm(self.gateway_id, version=PROVISION_API)
        workspace = self.arm(self.gateway_id + "/workspaces/default")
        print(json.dumps({
            "resourceType": gateway["type"],
            "provisioningState": gateway["properties"]["provisioningState"],
            "assetApiVersion": ASSET_API,
            "workspacePropertyNames": list(workspace.get("properties", {})),
        }, indent=2))

    @property
    def workspace_id(self) -> str:
        return self.gateway_id + "/workspaces/default"

    @property
    def foundry_id(self) -> str:
        if not self.state.get("foundryId"):
            raise DemoError("Deploy the playground Foundry account first.")
        return self.state["foundryId"]

    def configure(self) -> None:
        gateway = self.arm(self.gateway_id, version=PROVISION_API)
        if gateway["type"].lower() != "microsoft.apimanagement/aigateways":
            raise DemoError("The resource is not an AI Gateway.")
        self.arm(self.workspace_id)
        self.state["sku"] = gateway["sku"]["name"]
        self.state["hostname"] = gateway["properties"]["frontend"]["defaultHostname"]
        account = self.arm(self.foundry_id, version="2025-06-01")
        deployment = self.arm(
            self.foundry_id + "/deployments/" + self.config["model"], version="2025-06-01",
        )
        provider_id = self.workspace_id + "/modelProviders/foundry"
        self.arm(provider_id, "PUT", {"properties": {
            "kind": "Foundry", "name": "Playground Foundry models",
            "foundry": {
                "endpoint": account["properties"]["endpoint"],
                "resourceIds": [self.foundry_id],
                "authentication": {"kind": "ManagedIdentity"},
            },
        }})
        model_id = provider_id + "/models/" + self.config["model"]
        model = deployment["properties"]["model"]
        model_properties = {
            "displayName": self.config["model"], "apiFormat": "OpenAIChatCompletions",
            "deployment": {
                "resourceId": deployment["id"],
                "modelName": model["name"], "modelVersion": model["version"],
            },
            "supportedEndpoints": ["/openai/v1/chat/completions", "/openai/v1/responses"],
            "policies": [{
                "type": "tokenLimit", "count": 5000, "period": "minute",
                "counterKey": ["ipAddress"], "scope": "resource",
            }],
        }
        self.arm(model_id, "PUT", {"properties": model_properties})
        for name, policies in (
            (MODEL_ALIAS, []),
            (LIMITED_ALIAS, [{
                "type": "requestRateLimit", "scope": "resource",
                "callsPerPeriod": 1, "periodSeconds": 30, "counterKey": ["ipAddress"],
            }]),
        ):
            alias = self.update_alias(name, {
                "aliasName": name,
                "backendModels": [{"resourceId": model_id, "priority": 0, "weight": 1}],
                "policies": policies,
            })
            if not alias.get("properties", {}).get("gatewayEndpoints"):
                alias = self.arm(self.workspace_id + "/aliases/" + name)
            self.state.setdefault("modelEndpoints", {})[name] = runtime_endpoint(
                alias, "chat/completions", self.state["hostname"], documented_prefix="/default/models",
            )
        self.configure_telemetry()
        key_id = self.gateway_id + "/apiKeys/presenter"
        existing_status, _, _ = request(
            "GET", f"https://management.azure.com{key_id}?api-version={ASSET_API}",
            {"Authorization": f"Bearer {self.token()}"},
        )
        if existing_status == 404:
            self.arm(key_id, "PUT", {"properties": {
                "displayName": "Playground experiments", "state": "active",
                "primaryKey": secrets.token_hex(32), "secondaryKey": secrets.token_hex(32),
                "expirationDate": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            }})
        elif existing_status != 200:
            raise DemoError(f"Could not inspect the runtime credential: HTTP {existing_status}.")
        self.state["configured"] = True
        self.state["configuredAt"] = datetime.now(timezone.utc).isoformat()
        self.save()
        print("One backend deployment, normal/limited aliases, runtime access, and telemetry configured.")
        print("Runtime route uses the public portal model prefix and service-returned supported endpoints.")

    def configure_telemetry(self) -> None:
        insights_id = self.state["applicationInsightsId"]
        insights = self.arm(insights_id, version="2020-02-02")
        if insights["properties"].get("AzureMonitorWorkspaceIngestionMode", "").lower() != "enabled":
            self.arm(insights_id, "PATCH", {"properties": {
                "AzureMonitorWorkspaceIngestionMode": "Enabled",
            }}, version="2020-02-02-preview")
        deadline = time.monotonic() + 300
        rule = None
        while True:
            insights = self.arm(insights_id, version="2020-02-02")
            properties = insights["properties"]
            rule_id = properties.get("DataCollectionRuleResourceId") or properties.get("DataCollectionRuleId", "")
            if rule_id and not rule_id.startswith("/"):
                rules = self.arm(
                    self.prefix + "/providers/Microsoft.Insights/dataCollectionRules", version="2023-03-11",
                ).get("value", [])
                matches = [item for item in rules if item["properties"].get("immutableId") == rule_id]
                if len(matches) > 1:
                    raise DemoError("The native telemetry rule could not be identified uniquely.")
                rule_id = matches[0]["id"] if matches else ""
            if rule_id:
                rule = self.arm(rule_id, version="2023-03-11")
                endpoint_id = rule["properties"].get("dataCollectionEndpointId")
                if endpoint_id:
                    endpoint = self.arm(endpoint_id, version="2023-03-11")
                    endpoint_properties = endpoint["properties"]
                    logs = endpoint_properties.get("logsIngestion", {}).get("endpoint")
                    metrics = endpoint_properties.get("metricsIngestion", {}).get("endpoint")
                    if logs and metrics:
                        stream = "/dataCollectionRules/" + rule["properties"]["immutableId"] + "/streams"
                        properties.setdefault("OTLPMetricsEndpoint", metrics.rstrip("/") + stream + "/Custom-Metrics-Otel/otlp/v1/metrics")
                        properties.setdefault("OTLPLogsEndpoint", logs.rstrip("/") + stream + "/Microsoft-OTLP-Logs/otlp/v1/logs")
                        properties.setdefault("OTLPTracesEndpoint", logs.rstrip("/") + stream + "/Microsoft-OTLP-Traces/otlp/v1/traces")
            fields = ("OTLPMetricsEndpoint", "OTLPLogsEndpoint", "OTLPTracesEndpoint")
            if rule and all(properties.get(field) for field in fields):
                break
            if time.monotonic() >= deadline:
                raise DemoError("The native OTLP endpoints or Data Collection Rule did not become available within five minutes.")
            print("Waiting for Azure to provision the native OTLP destination.", flush=True)
            time.sleep(10)
        for field in fields:
            parsed = urlparse(properties[field])
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise DemoError("Azure returned an invalid native OTLP endpoint.")
        if not rule["id"].lower().startswith(self.prefix.lower() + "/"):
            raise DemoError("The native telemetry rule is outside the approved subscription.")
        gateway = self.arm(self.gateway_id, version=PROVISION_API)
        principal = gateway["identity"]["principalId"]
        role_definition = self.prefix + "/providers/Microsoft.Authorization/roleDefinitions/" + MONITOR_PUBLISHER_ROLE
        assignments = self.arm(
            rule["id"] + "/providers/Microsoft.Authorization/roleAssignments", version="2022-04-01",
        ).get("value", [])
        existing = [
            item for item in assignments
            if item["properties"].get("principalId", "").lower() == principal.lower()
            and item["properties"].get("roleDefinitionId", "").lower() == role_definition.lower()
            and item["properties"].get("scope", "").lower() == rule["id"].lower()
        ]
        if not existing:
            assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, rule["id"].lower() + principal.lower() + MONITOR_PUBLISHER_ROLE))
            assignment_id = rule["id"] + "/providers/Microsoft.Authorization/roleAssignments/" + assignment_name
            self.arm(assignment_id, "PUT", {"properties": {
                "principalId": principal, "principalType": "ServicePrincipal", "roleDefinitionId": role_definition,
            }}, version="2022-04-01")
            self.state["telemetryRoleAssignmentId"] = assignment_id
            self.save()
        exporter_id = self.workspace_id + "/telemetryExporters/appinsights"
        self.arm(exporter_id, "PUT", {"properties": {
            "kind": "OpenTelemetry", "payloadCapture": False,
            "applicationInsights": {"resourceId": insights["id"]},
            "openTelemetry": {
                "metricsEndpoint": properties["OTLPMetricsEndpoint"],
                "logsEndpoint": properties["OTLPLogsEndpoint"],
                "tracesEndpoint": properties["OTLPTracesEndpoint"],
                "credentials": {"managedIdentity": {"resource": "https://monitor.azure.com"}},
            },
        }}, version=PORTAL_ASSET_API)
        exporter = self.arm(exporter_id, version=PORTAL_ASSET_API)
        stored = exporter["properties"]
        if (
            stored.get("kind") not in ("OpenTelemetry", "openTelemetry")
            or stored.get("applicationInsights", {}).get("resourceId", "").lower() != insights["id"].lower()
            or stored.get("payloadCapture") is not False
            or any(
                stored.get("openTelemetry", {}).get(target) != properties[source]
                for target, source in (
                    ("metricsEndpoint", "OTLPMetricsEndpoint"),
                    ("logsEndpoint", "OTLPLogsEndpoint"),
                    ("tracesEndpoint", "OTLPTracesEndpoint"),
                )
            )
        ):
            raise DemoError("The native telemetry exporter did not retain its type, destination, or payload capture setting.")
        self.state["nativeOtlp"] = {
            "configured": True, "dataCollectionRuleId": rule["id"], "exporterId": exporter_id,
            "payloadCapture": False, "authentication": "ManagedIdentity",
        }
        self.save()
        print("Native OTLP metrics, logs, and traces configured with managed identity. Payload capture remains disabled.")

    def update_alias(self, name: str, properties: dict) -> dict:
        if name not in (MODEL_ALIAS, LIMITED_ALIAS, ROUTING_ALIAS):
            raise DemoError("Only the owned demonstration aliases can be configured.")
        alias_id = self.workspace_id + "/aliases/" + name
        url = f"https://management.azure.com{alias_id}?api-version={PORTAL_ASSET_API}"
        headers = {"Authorization": "Bearer " + self.token()}
        status, response_headers, current = request("GET", url, headers)
        if status == 404:
            method = "PUT"
            headers["If-None-Match"] = "*"
        elif status == 200:
            method = "PATCH"
            etag = next((value for key, value in response_headers.items() if key.lower() == "etag"), None)
            if not etag:
                raise DemoError("The alias did not return an ETag. Refusing an unprotected update.")
            headers["If-Match"] = etag
        else:
            raise DemoError(f"The alias could not be read before its update: HTTP {status}.")
        status, _, _ = request(method, url, headers, {"properties": properties})
        if status == 412:
            raise DemoError("The alias changed during configuration. Refresh and retry.")
        if status not in (200, 201, 202):
            raise DemoError(f"The alias configuration failed: HTTP {status}.")
        return self.arm(alias_id, version=PORTAL_ASSET_API)

    def delete_owned_gateway_asset(self, asset_id: str) -> None:
        approved_prefix = self.gateway_id.lower() + "/workspaces/default/"
        group = self.config["resourceGroupName"]
        gateway_prefix = f"{self.prefix}/resourceGroups/{group}/providers/Microsoft.ApiManagement/aigateways/"
        if (
            not self.gateway_id.lower().startswith(gateway_prefix.lower())
            or not asset_id.lower().startswith(approved_prefix)
        ):
            raise DemoError("Deletion is restricted to this AI Gateway's default workspace.")
        self.arm(asset_id, "DELETE", version=PORTAL_ASSET_API, allowed=(200, 202, 204, 404))

    def cleanup_legacy_telemetry(self) -> None:
        current = self.arm(self.workspace_id + "/telemetryExporters/appinsights", version=PORTAL_ASSET_API)
        if (
            current["properties"].get("kind") not in ("OpenTelemetry", "openTelemetry")
            or current["properties"].get("applicationInsights", {}).get("resourceId", "").lower()
            != self.state["applicationInsightsId"].lower()
        ):
            raise DemoError("The native exporter must be verified before removing the obsolete configuration.")
        self.delete_owned_gateway_asset(self.workspace_id + "/telemetryExporters/application-insights")
        print("Removed only the obsolete gateway exporter configuration. Application Insights and stored telemetry remain unchanged.")

    def configure_aliases(self) -> None:
        model_id = self.workspace_id + "/modelProviders/foundry/models/" + self.config["model"]
        self.arm(model_id)
        limited_id = self.workspace_id + "/aliases/" + LIMITED_ALIAS
        limited = self.arm(limited_id, version=PORTAL_ASSET_API)
        policies = limited["properties"].get("policies", [])
        managed_limit = {
            "type": "requestRateLimit", "scope": "resource",
            "callsPerPeriod": 1, "periodSeconds": 30, "counterKey": ["ipAddress"],
        }
        policies = [
            policy for policy in policies
            if not (
                policy.get("type") == "requestRateLimit"
                and policy.get("callsPerPeriod") == 1 and policy.get("periodSeconds") == 30
                and policy.get("counterKey") == ["ipAddress"]
            )
        ] + [managed_limit]
        alias = self.update_alias(LIMITED_ALIAS, {
            "backendModels": [{"resourceId": model_id, "priority": 0, "weight": 1}],
            "policies": policies,
        })
        stored = alias["properties"]
        if (
            len(stored.get("backendModels", [])) != 1
            or stored["backendModels"][0]["resourceId"].lower() != model_id.lower()
            or not any(
                all(policy.get(key) == value for key, value in managed_limit.items())
                for policy in stored.get("policies", [])
            )
        ):
            raise DemoError("The limited alias did not retain its backend or request limit.")
        print("The normal and limited aliases share one catalog model. The request limit is attached to the limited alias.")
        self.rate()
        obsolete_id = model_id + "-limited"
        references = self.arm(self.workspace_id + "/aliases", version=PORTAL_ASSET_API).get("value", [])
        if any(
            backend["resourceId"].lower() == obsolete_id.lower()
            for item in references for backend in item["properties"].get("backendModels", [])
        ):
            raise DemoError("Another alias still uses the redundant catalog entry. No catalog entry was removed.")
        self.delete_owned_gateway_asset(obsolete_id)
        self.state["aliasPolicies"] = {"verified": True, "sharedCatalogModel": model_id, "requestLimitAlias": LIMITED_ALIAS}
        self.save()
        print("Alias isolation and recovery passed. Removed only the redundant gateway catalog entry.")

    def configure_routing(self) -> None:
        names = self.config.get("routingModels", [])
        if (
            not isinstance(names, list) or len(names) != 3
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in names)
            or len(set(names)) != 3
        ):
            raise DemoError("Configure exactly three distinct existing chat deployments in routingModels.")
        targets = []
        for name in names:
            deployment = self.arm(self.foundry_id + "/deployments/" + name, version="2025-06-01")
            model = deployment["properties"]["model"]
            if model.get("format") != "OpenAI" or deployment["properties"].get("provisioningState") != "Succeeded":
                raise DemoError("Routing targets must be available OpenAI-compatible deployments.")
            model_id = self.workspace_id + "/modelProviders/foundry/models/" + name
            if name != self.config["model"]:
                self.arm(model_id, "PUT", {"properties": {
                    "displayName": name, "apiFormat": "OpenAIChatCompletions",
                    "deployment": {
                        "resourceId": deployment["id"], "modelName": model["name"], "modelVersion": model["version"],
                    },
                    "supportedEndpoints": ["/openai/v1/chat/completions", "/openai/v1/responses"],
                    "policies": [{
                        "type": "tokenLimit", "count": 5000, "period": "minute",
                        "counterKey": ["ipAddress"], "scope": "resource",
                    }],
                }})
            targets.append({"deployment": name, "model": model["name"], "resourceId": model_id})
        if len({target["model"] for target in targets}) != len(targets):
            raise DemoError("Distinct model names are required to identify selected backends from real replies.")
        alias = self.update_alias(ROUTING_ALIAS, {
            "aliasName": ROUTING_ALIAS,
            "backendModels": [
                {"resourceId": target["resourceId"], "priority": 0, "weight": weight}
                for target, weight in zip(targets, ROUTING_WEIGHTS)
            ],
        })
        self.state["modelEndpoints"][ROUTING_ALIAS] = runtime_endpoint(
            alias, "chat/completions", self.state["hostname"], documented_prefix="/default/models",
        )
        self.state["routingModels"] = targets
        self.save()
        print("Created one routing alias over the three playground model deployments.")

    @recorded_stage("routing-weighted")
    def routing_weighted(self) -> None:
        self.routing("weighted")

    @recorded_stage("routing-priority")
    def routing_priority(self) -> None:
        self.routing("priority")

    def apply_routing_mode(self, mode: str) -> list[dict]:
        if mode not in ("weighted", "priority"):
            raise DemoError("Routing mode must be weighted or priority.")
        targets = self.state.get("routingModels", [])
        if len(targets) != 3:
            raise DemoError("Configure the dedicated routing alias first.")
        approved_prefix = self.workspace_id.lower() + "/modelproviders/"
        if any(not target.get("resourceId", "").lower().startswith(approved_prefix) for target in targets):
            raise DemoError("Routing targets must stay in this AI Gateway workspace.")
        backends = [
            {
                "resourceId": target["resourceId"], "weight": weight,
                "priority": 0 if mode == "weighted" or index == 2 else 1,
            }
            for index, (target, weight) in enumerate(zip(targets, ROUTING_WEIGHTS))
        ]
        alias = self.update_alias(ROUTING_ALIAS, {"backendModels": backends})
        EVENTS.configuration("Applied routing alias", alias["properties"])
        stored = alias["properties"].get("backendModels", [])
        for expected in backends:
            match = next((item for item in stored if item["resourceId"].lower() == expected["resourceId"].lower()), None)
            if not match or match.get("weight") != expected["weight"] or match.get("priority") != expected["priority"]:
                raise DemoError("The routing alias did not retain the requested weights and priorities.")
        self.state["routingMode"] = mode
        self.save()
        return backends

    @recorded_stage("reset-demo")
    def reset_demo(self) -> None:
        self.apply_routing_mode("weighted")
        print("Restored weighted routing. Waiting 30 seconds for configuration propagation.", flush=True)
        time.sleep(30)
        for key in (
            "modelChecks", "lastModelCheckAt", "rateChecks", "mcpChecks",
            "activityChecks", "telemetryChecks", "routingChecks",
            "nativeTokenGap", "verified", "coreChecksPassed", "setupStatus", "verifiedAt",
        ):
            self.state.pop(key, None)
        self.state["presenterResetAt"] = datetime.now(timezone.utc).isoformat()
        self.save()
        print("Playground reset. Azure monitoring data remains available.")

    def routing(self, mode: str) -> None:
        backends = self.apply_routing_mode(mode)
        targets = self.state["routingModels"]
        print("Waiting 30 seconds for the routing configuration to propagate before sampling.", flush=True)
        time.sleep(30)
        key = self.runtime_key()
        samples = 50 if mode == "weighted" else 10
        transition_requests = 0
        for attempt in range(3):
            counts = {target["model"]: 0 for target in targets}
            for _ in range(samples):
                status, _, payload = self.model_request(
                    ROUTING_ALIAS, key, key_header="api-key", prompt=ROUTING_PROMPT, max_output_tokens=64,
                )
                result = self.model_result(status, payload)
                actual = result.get("model", "")
                if not isinstance(actual, str):
                    raise DemoError("A routing reply did not include a model name.")
                matched = next((
                    name for name in sorted(counts, key=len, reverse=True)
                    if actual == name or actual.startswith(name + "-")
                ), None)
                if matched is None:
                    raise DemoError("A routing reply did not identify one of the configured backend models.")
                counts[matched] += 1
            settled = (
                counts[targets[2]["model"]] == samples if mode == "priority"
                else all(count > 0 for count in counts.values())
            )
            if settled:
                break
            if attempt == 2:
                raise DemoError("The routing sample did not demonstrate the configured selection after three attempts.")
            transition_requests += samples
            print("The sample did not demonstrate the requested routing. Waiting 30 seconds before another sample.", flush=True)
            time.sleep(30)
        result = {
            "mode": mode, "requests": samples, "transitionRequests": transition_requests, "alias": ROUTING_ALIAS,
            "targets": [
                {"model": target["model"], "weight": backend["weight"], "priority": backend["priority"], "requests": counts[target["model"]]}
                for target, backend in zip(targets, backends)
            ],
        }
        self.state.setdefault("routingChecks", {})["routing-" + mode] = result
        self.save()
        print(json.dumps(result, indent=2))

    def runtime_key(self) -> str:
        result = self.arm(self.gateway_id + "/apiKeys/presenter/listSecrets", "POST")
        key = result.get("primaryKey")
        if not isinstance(key, str) or len(key) < 20:
            raise DemoError("The runtime credential is unavailable.")
        return key

    def model_request(
        self, alias: str, key: str | None, key_header: str = "Authorization", *,
        prompt: str = MODEL_PROMPTS[0], max_output_tokens: int = 220,
    ) -> tuple[int, dict, object]:
        endpoint = self.state.get("modelEndpoints", {}).get(alias)
        if not endpoint:
            raise DemoError("Configure the model invocation endpoint first.")
        require_runtime_url(endpoint, self.state["hostname"])
        headers = {
            "x-correlation-id": str(uuid.uuid4()),
            "traceparent": f"00-{uuid.uuid4().hex}-{secrets.token_hex(8)}-01",
        }
        if key is not None:
            if key_header == "Authorization":
                headers["Authorization"] = f"Bearer {key}"
            elif key_header == "api-key":
                headers["api-key"] = key
            else:
                raise DemoError("Unsupported model credential header.")
        if urlparse(endpoint).path.endswith("/chat/completions"):
            body = {
                "model": alias,
                "messages": [{"role": "user", "content": prompt}],
                "max_completion_tokens": max_output_tokens, "reasoning_effort": "none",
            }
        elif urlparse(endpoint).path.endswith("/responses"):
            body = {
                "model": alias, "input": prompt,
                "max_output_tokens": max_output_tokens, "reasoning": {"effort": "none"},
            }
        else:
            raise DemoError("The configured model endpoint is not a supported demonstration API.")
        return request("POST", endpoint, headers, body)

    @staticmethod
    def model_result(status: int, payload: object) -> dict:
        if status != 200 or not isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code", "Unknown") if isinstance(error, dict) else "GatewayError"
            raise DemoError(f"Model invocation failed: HTTP {status}, code {code}.")
        texts = [
            content.get("text", "")
            for item in payload.get("output", [])
            if isinstance(item, dict)
            for content in item.get("content", [])
            if isinstance(content, dict) and content.get("type") == "output_text"
            and isinstance(content.get("text"), str)
        ]
        texts.extend(
            item.get("message", {}).get("content", "")
            for item in payload.get("choices", [])
            if isinstance(item, dict) and isinstance(item.get("message"), dict)
            and isinstance(item["message"].get("content"), str)
        )
        usage = payload.get("usage", {})
        if not any(text.strip() for text in texts):
            raise DemoError("The model returned no answer.")
        if not isinstance(usage, dict) or type(usage.get("total_tokens")) is not int or usage["total_tokens"] <= 0:
            raise DemoError("The response did not contain non-zero token usage.")
        return {
            "httpStatus": status, "answerPresent": True,
            "model": payload.get("model"), "inputTokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "outputTokens": usage.get("output_tokens", usage.get("completion_tokens")), "totalTokens": usage["total_tokens"],
        }

    def capture_alias(self, name: str) -> None:
        if not EVENTS.enabled:
            return
        alias = self.arm(self.workspace_id + "/aliases/" + name, version=PORTAL_ASSET_API)
        EVENTS.configuration("Alias: " + name, alias["properties"])
        for target in alias["properties"].get("backendModels", []):
            if not target["resourceId"].lower().startswith(self.workspace_id.lower() + "/modelproviders/"):
                raise DemoError("The alias backend is outside the approved gateway workspace.")
            model = self.arm(target["resourceId"])
            EVENTS.configuration("Shared backend policies: " + model["name"], {
                "model": model["properties"].get("displayName", model["name"]),
                "policies": model["properties"].get("policies", []),
            })

    @recorded_stage("model-access")
    def smoke(self) -> None:
        self.capture_alias(MODEL_ALIAS)
        key = self.runtime_key()
        checks = []
        for label, credential in (("missingCredential", None), ("invalidCredential", "invalid-demo-credential")):
            EVENTS.expect("Missing gateway credential" if credential is None else "Invalid gateway credential", (401, 403))
            status, _, _ = self.model_request(MODEL_ALIAS, credential)
            if status not in (401, 403):
                raise DemoError(f"{label} did not produce an authentication rejection: HTTP {status}.")
            checks.append({"check": label, "httpStatus": status})
        for prompt in MODEL_PROMPTS:
            status, _, payload = self.model_request(MODEL_ALIAS, key, prompt=prompt)
            checks.append({"check": "model", **self.model_result(status, payload)})
        self.state["modelChecks"] = checks
        self.state["lastModelCheckAt"] = datetime.now(timezone.utc).isoformat()
        self.save()
        print(json.dumps(checks, indent=2))

    @recorded_stage("request-limits")
    def rate(self) -> None:
        self.capture_alias(LIMITED_ALIAS)
        self.state.pop("rateChecks", None)
        self.save()
        key = self.runtime_key()
        first, headers, payload = self.model_request(
            LIMITED_ALIAS, key, key_header="api-key", prompt=LIMIT_PROMPT, max_output_tokens=64,
        )
        if first == 429:
            wait = retry_seconds(headers)
            print("The previous rate window is active. Waiting for its documented retry period.")
            time.sleep(wait)
            first, _, payload = self.model_request(
                LIMITED_ALIAS, key, key_header="api-key", prompt=LIMIT_PROMPT, max_output_tokens=64,
            )
        self.model_result(first, payload)
        allowed_count = 1
        blocked = 200
        for _ in range(4):
            blocked, headers, payload = self.model_request(
                LIMITED_ALIAS, key, key_header="api-key", prompt=LIMIT_PROMPT, max_output_tokens=64,
            )
            if blocked == 429:
                break
            self.model_result(blocked, payload)
            allowed_count += 1
        if blocked != 429:
            raise DemoError("The isolated rate policy did not reject any call within the five-request test bound.")
        control_status, _, control_payload = self.model_request(
            MODEL_ALIAS, key, key_header="api-key", prompt=LIMIT_PROMPT, max_output_tokens=64,
        )
        self.model_result(control_status, control_payload)
        print(f"Rate control allowed {allowed_count} request(s), then returned 429. Waiting for recovery.")
        time.sleep(retry_seconds(headers))
        recovered, _, payload = self.model_request(
            LIMITED_ALIAS, key, key_header="api-key", prompt=LIMIT_PROMPT, max_output_tokens=64,
        )
        self.model_result(recovered, payload)
        result = {
            "allowed": first, "allowedBeforeBlock": allowed_count,
            "limited": blocked, "recovered": recovered, "counter": "ipAddress",
            "configuredCallsPerPeriod": 1, "periodSeconds": 30,
            "unlimitedAliasDuringBlock": control_status,
        }
        self.state["rateChecks"] = result
        self.save()
        print(json.dumps(result, indent=2))

    def configure_mcp(self) -> None:
        account = self.arm(self.foundry_id, version="2025-06-01")
        backend_url = account["properties"]["endpoint"].rstrip("/")
        parsed = urlparse(backend_url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith((".cognitiveservices.azure.com", ".openai.azure.com", ".services.ai.azure.com"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)
        ):
            raise DemoError("Foundry returned an invalid model-tool endpoint.")
        spec = {
            "openapi": "3.0.1",
            "info": {"title": "Playground model tools", "version": "1.0.0"},
            "servers": [{"url": backend_url}],
            "paths": {"/openai/v1/chat/completions": {"post": {
                "operationId": "summarize_text",
                "summary": "Summarize text with the playground's own model.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object", "required": ["model", "messages"],
                        "properties": {
                            "model": {"type": "string", "enum": [self.config["model"]]},
                            "messages": {"type": "array", "minItems": 1, "maxItems": 1, "items": {
                                "type": "object", "required": ["role", "content"],
                                "properties": {
                                    "role": {"type": "string", "enum": ["user"]},
                                    "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                                },
                            }},
                            "max_completion_tokens": {"type": "integer", "minimum": 1, "maximum": 128, "default": 96},
                            "reasoning_effort": {"type": "string", "enum": ["none"]},
                        },
                    }}},
                },
                "responses": {"200": {"description": "Model answer and token usage"}},
            }}},
        }
        tool_id = self.workspace_id + "/toolServers/model-tools"
        tool = self.arm(tool_id, "PUT", {"properties": {
            "displayName": "Playground model tools", "type": "openApi", "accessState": "active",
            "failureMode": "failClosed",
            "subscriptionRequired": True,
            "endpoints": [{
                "id": "model", "namespace": "model", "kind": "openApi",
                "openApi": {"specSource": {
                    "type": "inline", "contentBase64": base64.b64encode(json.dumps(spec).encode()).decode(),
                }},
                "credentials": {
                    "type": "managedIdentity",
                    "managedIdentity": {"resource": "https://cognitiveservices.azure.com"},
                },
                "tools": ["summarize_text"], "required": True,
            }],
            "policies": [],
        }})
        if not tool.get("properties", {}).get("gatewayEndpoints"):
            tool = self.arm(tool_id)
        endpoints = tool.get("properties", {}).get("gatewayEndpoints", [])
        if endpoints:
            self.state["mcpEndpoint"] = runtime_endpoint(tool, "mcp", self.state["hostname"])
        else:
            self.state["mcpEndpoint"] = require_runtime_url(
                f"https://{self.state['hostname']}/default/toolservers/model-tools/mcp",
                self.state["hostname"],
            )
        self.state["mcpSource"] = "OpenAPI over the playground Foundry model with managed identity"
        self.save()
        print("The MCP summarization tool uses the playground's own Foundry model and managed identity.")

    @recorded_stage("ai-gateway-mcp")
    def mcp(self) -> None:
        endpoint = self.state.get("mcpEndpoint")
        if not endpoint:
            raise DemoError("Configure MCP first.")
        require_runtime_url(endpoint, self.state["hostname"])
        if EVENTS.enabled:
            server = self.arm(self.workspace_id + "/toolServers/model-tools")["properties"]
            EVENTS.configuration("MCP server settings", {
                "displayName": server.get("displayName"), "type": server.get("type"),
                "failureMode": server.get("failureMode"), "policies": server.get("policies", []),
                "endpoints": [{
                    key: value for key, value in item.items()
                    if key in ("id", "namespace", "kind", "tools", "credentials")
                } for item in server.get("endpoints", [])],
            })
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "ai-gateway-playground", "version": "1.0"},
            },
        }
        auth_checks = []
        for label, headers in (
            ("missingCredential", {}),
            ("invalidCredential", {"api-key": "invalid-demo-credential"}),
        ):
            EVENTS.expect("Missing MCP credential" if not headers else "Invalid MCP credential", (401, 403))
            status, _, _ = request(
                "POST", endpoint, {"Accept": "application/json, text/event-stream", **headers}, initialize,
            )
            if status not in (401, 403):
                raise DemoError(f"MCP {label} did not reject authentication: HTTP {status}.")
            auth_checks.append({"check": label, "httpStatus": status})
        headers = {
            "api-key": self.runtime_key(),
            "Accept": "application/json, text/event-stream",
        }
        check = {**model_tool_call(endpoint, headers, self.config["model"]), "authentication": auth_checks}
        self.state["mcpChecks"] = check
        self.save()
        print(json.dumps(check, indent=2))

    @recorded_stage("native-tokens")
    def telemetry(self) -> None:
        if self.state.get("nativeOtlp", {}).get("configured"):
            self.telemetry_otlp()
            return
        workspace = self.arm(self.state["logWorkspaceId"], version="2023-09-01")
        query = self.token_query()
        deadline = time.monotonic() + 300
        while True:
            status, _, payload = request(
                "POST",
                "https://api.loganalytics.azure.com/v1/workspaces/"
                + workspace["properties"]["customerId"] + "/query",
                {"Authorization": f"Bearer {self.token('https://api.loganalytics.io')}"},
                {"query": query, "timespan": "PT1H"},
            )
            if status != 200 or not isinstance(payload, dict) or payload.get("error"):
                raise DemoError(f"The telemetry query failed: HTTP {status}.")
            table = payload["tables"][0]
            rows = table["rows"]
            record = dict(zip([column["name"] for column in table["columns"]], rows[0])) if rows else {}
            if record.get("Records", 0) > 0 and record.get("InputTokens", 0) > 0 and record.get("OutputTokens", 0) > 0:
                self.state.pop("nativeTokenGap", None)
                self.state["telemetryChecks"] = record
                self.save()
                print(json.dumps(record, indent=2))
                return
            if record.get("Records", 0) > 0 and record.get("InputTokens") == 0 and record.get("OutputTokens") == 0:
                self.state["nativeTokenGap"] = {
                    "status": "failed", "observed": record,
                    "reason": "Native token fields are zero despite non-zero usage in model responses.",
                }
                self.save()
                raise NativeTokenGap(
                    "Native gateway token records contain zero input and output usage. "
                    "Model responses report non-zero usage. No substitute telemetry was written."
                )
            if time.monotonic() >= deadline:
                raise DemoError("No non-zero gateway token telemetry arrived within five minutes.")
            print("Waiting for real Application Insights token measurements.")
            time.sleep(20)

    def token_query(self) -> str:
        return (
            "AppRequests | where TimeGenerated > ago(1h) "
            f"| where _ResourceId =~ '{self.state['applicationInsightsId']}' "
            "| extend InputTokens=todouble(Measurements['gen_ai.usage.input_tokens']), "
            "OutputTokens=todouble(Measurements['gen_ai.usage.output_tokens']) "
            "| where isnotnull(InputTokens) and isnotnull(OutputTokens) "
            "| summarize Records=count(), InputTokens=sum(InputTokens), "
            "OutputTokens=sum(OutputTokens), LastSeen=max(TimeGenerated)"
        )

    def token_selector(self, metric: str = NATIVE_TOKEN_METRIC) -> str:
        return (
            '{__name__="' + metric + '",'
            '"microsoft.appresourceid"=' + json.dumps(self.state["applicationInsightsId"].lower()) + ","
            '"azure.ai_gateway.workspace"="default"}'
        )

    def telemetry_otlp(self) -> None:
        insights = self.arm(self.state["applicationInsightsId"], version="2020-02-02")
        workspace_id = insights["properties"].get("AzureMonitorWorkspaceResourceId")
        if not workspace_id:
            raise DemoError("Application Insights has no native metrics workspace.")
        workspace = self.arm(workspace_id, version="2023-04-03")
        endpoint = workspace["properties"].get("metrics", {}).get("prometheusQueryEndpoint", "")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".prometheus.monitor.azure.com")
            or parsed.username or parsed.password or parsed.port not in (None, 443)
        ):
            raise DemoError("Azure returned an invalid native metrics query endpoint.")
        EVENTS.observe_host(parsed.hostname, "AI Gateway metrics")
        EVENTS.identifier(parsed.hostname, "<native-metrics>")
        query = "last_over_time(" + self.token_selector() + "[1h])"
        EVENTS.configuration("Native token query (PromQL)", {
            "metric": NATIVE_TOKEN_METRIC, "query": query,
            "meaning": "Latest cumulative counters from recent native samples; not a billing-period total.",
        })
        url = endpoint.rstrip("/") + "/api/v1/query?" + urlencode({"query": query})
        deadline = time.monotonic() + 300
        while True:
            status, _, payload = request("GET", url, {
                "Authorization": "Bearer " + self.token("https://prometheus.monitor.azure.com"),
            })
            if status != 200 or not isinstance(payload, dict):
                raise DemoError(f"The native OTLP metric query failed: HTTP {status}.")
            result = native_token_totals(payload)
            if result["InputTokens"] > 0 and result["OutputTokens"] > 0:
                cost_query = "last_over_time(" + self.token_selector(NATIVE_COST_METRIC) + "[1h])"
                EVENTS.configuration("Native estimated cost query (PromQL)", {
                    "metric": NATIVE_COST_METRIC, "query": cost_query,
                    "meaning": "Latest cumulative gateway-estimated model costs seen within one hour; not hourly spend or billed charges.",
                })
                cost_status, _, cost_payload = request("GET", endpoint.rstrip("/") + "/api/v1/query?" + urlencode({"query": cost_query}), {
                    "Authorization": "Bearer " + self.token("https://prometheus.monitor.azure.com"),
                })
                if cost_status != 200 or not isinstance(cost_payload, dict):
                    raise DemoError(f"The native estimated cost query failed: HTTP {cost_status}.")
                result["Costs"] = native_cost_totals(cost_payload)
                self.state.pop("nativeTokenGap", None)
                self.state["telemetryChecks"] = result
                self.save()
                print(json.dumps(result, indent=2))
                return
            if time.monotonic() >= deadline:
                raise DemoError("No non-zero native OTLP token values arrived within five minutes.")
            print("Waiting for native OTLP token usage. No substitute measurements are written.", flush=True)
            time.sleep(15)

    @recorded_stage("native-activity")
    def activity(self) -> None:
        workspace = self.arm(self.state["logWorkspaceId"], version="2023-09-01")
        query = self.activity_query()
        EVENTS.identifier(workspace["properties"]["customerId"], "<workspace-id>")
        EVENTS.configuration("Native request query (KQL)", {"query": query})
        deadline = time.monotonic() + 300
        while True:
            status, _, payload = request(
                "POST",
                "https://api.loganalytics.azure.com/v1/workspaces/"
                + workspace["properties"]["customerId"] + "/query",
                {"Authorization": f"Bearer {self.token('https://api.loganalytics.io')}"},
                {"query": query, "timespan": "PT15M"},
            )
            if status != 200 or not isinstance(payload, dict) or payload.get("error"):
                raise DemoError(f"The gateway activity query failed: HTTP {status}.")
            table = payload["tables"][0]
            rows = table["rows"]
            record = dict(zip([column["name"] for column in table["columns"]], rows[0])) if rows else {}
            if record.get("Requests", 0) > 0 and record.get("LastSeen"):
                self.state["activityChecks"] = record
                self.save()
                print(json.dumps({"nativeGatewayActivity": record}, indent=2))
                return
            if time.monotonic() >= deadline:
                raise DemoError("No native gateway activity arrived in Application Insights within five minutes.")
            print("Waiting for native gateway activity. Application Insights ingestion can lag.")
            time.sleep(20)

    def monitoring(self) -> None:
        self.activity()
        self.telemetry()

    def activity_query(self) -> str:
        if self.state.get("nativeOtlp", {}).get("configured"):
            return (
                "OTelSpans | where TimeGenerated > ago(15m) | where Kind =~ 'Server' "
                f"| where _ResourceId =~ '{self.state['applicationInsightsId']}' "
                "| summarize Requests=count(), LastSeen=max(TimeGenerated)"
            )
        return (
            "AppRequests | where TimeGenerated > ago(15m) "
            f"| where _ResourceId =~ '{self.state['applicationInsightsId']}' "
            "| summarize Requests=count(), LastSeen=max(TimeGenerated)"
        )

    def queries(self) -> None:
        if not self.state.get("applicationInsightsId"):
            raise DemoError("Restore the approved local deployment state before preparing queries.")
        print("PREPARE PRIVATELY: these queries contain your deployment resource ID.")
        print("\nNATIVE ACTIVITY - KQL IN WORKSPACE LOGS\n" + self.activity_query().replace(" | ", "\n| "))
        if self.state.get("nativeOtlp", {}).get("configured"):
            print("\nNATIVE TOKEN REPORTING - PROMQL IN THE NATIVE METRICS WORKSPACE")
            print("last_over_time(" + self.token_selector() + "[1h])")
            print("This returns the latest cumulative counters, not a billing-period total.")
        else:
            print("\nNATIVE TOKEN REPORTING - LEGACY REQUEST FIELDS (KQL IN WORKSPACE LOGS)\n" + self.token_query().replace(" | ", "\n| "))

    def show(self) -> None:
        required = ("hostname", "gatewayName", "sku", "applicationInsightsId", "foundryId", "logWorkspaceId", "modelEndpoints", "mcpEndpoint")
        if any(not self.state.get(name) for name in required):
            raise DemoError("Local deployment state is incomplete. Restore the approved operator state before presenting.")
        print("PLAYGROUND MAP")
        print("New AI Gateway and Application Insights: " + self.config["resourceGroupName"])
        print("AI Gateway resource type: Microsoft.ApiManagement/aigateways")
        print("AI Gateway SKU: " + self.state["sku"])
        print("Foundry models and Log Analytics belong to this playground.")
        print("\nOPEN THESE LINKS IN A BROWSER")
        print("AI Gateway portal: https://ai.gateway.azure.com")
        print("Select gateway: " + self.state["gatewayName"])
        print("New Application Insights: https://portal.azure.com/#resource" + self.state["applicationInsightsId"] + "/overview")
        print("Foundry: https://portal.azure.com/#resource" + self.foundry_id + "/overview")
        print("Workspace Logs: https://portal.azure.com/#resource" + self.state["logWorkspaceId"] + "/logs")
        print("\nAPI ADDRESSES - USE THE SCRIPTS, NOT BROWSER NAVIGATION")
        print("Gateway API base: https://" + self.state["hostname"])
        print("Model endpoint: " + self.state["modelEndpoints"][MODEL_ALIAS])
        print("MCP endpoint: " + self.state["mcpEndpoint"])
        checks = ("modelChecks", "rateChecks", "mcpChecks", "activityChecks", "telemetryChecks")
        print(json.dumps({"completedChecks": [name for name in checks if name in self.state]}, indent=2))
        if self.state.get("nativeTokenGap"):
            print("NATIVE TOKEN CHECK FAILED: token fields are zero. This is not a full verification pass.")

    def inventory(self) -> None:
        owned = [
            self.gateway_id, self.state["applicationInsightsId"], self.foundry_id,
            self.state["logWorkspaceId"], self.state["modelAccessId"],
            self.gateway_id + "/apiKeys/presenter",
            self.workspace_id + "/modelProviders/foundry",
            self.workspace_id + "/modelProviders/foundry/models/" + self.config["model"],
            self.workspace_id + "/aliases/" + MODEL_ALIAS,
            self.workspace_id + "/aliases/" + LIMITED_ALIAS,
            self.state.get("nativeOtlp", {}).get("exporterId", self.workspace_id + "/telemetryExporters/application-insights"),
            self.workspace_id + "/toolServers/model-tools",
        ]
        if self.state.get("routingModels"):
            owned.extend(target["resourceId"] for target in self.state["routingModels"] if target["deployment"] != self.config["model"])
            owned.append(self.workspace_id + "/aliases/" + ROUTING_ALIAS)
        inventory = {
            "createdResourceGroup": self.config["resourceGroupName"],
            "ownedResources": owned,
            "modelDeployments": [self.foundry_id + "/deployments/" + item["name"] for item in self.config["modelDeployments"]],
            "telemetryRoleAssignmentId": self.state.get("telemetryRoleAssignmentId"),
            "managedTelemetryRuleId": self.state.get("nativeOtlp", {}).get("dataCollectionRuleId"),
            "cleanupRequiresApproval": True,
        }
        path = ROOT / ".azure" / "ownership.json"
        path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
        print("Resource ownership recorded in ignored .azure/ownership.json; no deletion performed.")

    def verify(self) -> int:
        self.state["verified"] = False
        self.state["coreChecksPassed"] = False
        self.state.pop("setupStatus", None)
        self.save()
        for check in (self.smoke, self.rate, self.mcp, self.activity):
            check()
        if self.config.get("routingModels"):
            self.routing_priority()
            self.routing_weighted()
        self.inventory()
        try:
            self.telemetry()
        except NativeTokenGap:
            if self.config.get("allowNativeTokenGap") is not True:
                raise
            self.state["coreChecksPassed"] = True
            self.state["setupStatus"] = "accepted-with-native-token-gap"
            self.state["verifiedAt"] = datetime.now(timezone.utc).isoformat()
            self.save()
            print(
                "CORE CHECKS PASSED; NATIVE TOKEN CHECK FAILED. "
                "The user approved presenting this gap. No substitute telemetry was added. Exit code: 2."
            )
            return 2
        self.state["verified"] = True
        self.state["coreChecksPassed"] = True
        self.state["setupStatus"] = "fully-verified"
        self.state["verifiedAt"] = datetime.now(timezone.utc).isoformat()
        self.save()
        print("Model, authentication, rate control, MCP, routing, and native telemetry checks passed.")
        return 0


def model_tool_call(endpoint: str, headers: dict[str, str], model: str) -> dict:
    result, response_headers = mcp_rpc(endpoint, headers, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "ai-gateway-playground", "version": "1.0"},
        },
    })
    version = result.get("protocolVersion")
    if not version:
        raise DemoError("MCP initialization did not return a protocol version.")
    headers = {**headers, "MCP-Protocol-Version": version}
    for name, value in response_headers.items():
        if name.lower() == "mcp-session-id":
            headers["Mcp-Session-Id"] = value
    mcp_rpc(endpoint, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    result, _ = mcp_rpc(endpoint, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = result.get("tools", [])
    if len(tools) != 1 or not tools[0].get("name", "").endswith("summarize_text"):
        raise DemoError("The MCP server did not expose exactly the permitted model tool.")
    result, _ = mcp_rpc(endpoint, headers, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": tools[0]["name"], "arguments": {
            "model": model,
            "messages": [{"role": "user", "content": (
                "Summarize: A project has three open tasks. Two need owners and one needs a due date. "
                "State the next action in one sentence."
            )}],
            "max_completion_tokens": 96, "reasoning_effort": "none",
        }},
    })
    try:
        model_check = tool_model_result(result)
    except DemoError as error:
        EVENTS.invalid_exchange(str(error))
        raise
    return {
        "initialized": True, "toolCount": len(tools), "tool": tools[0]["name"],
        "invocationSucceeded": True, **model_check,
    }


def mcp_rpc(url: str, headers: dict[str, str], body: dict) -> tuple[dict, dict]:
    headers = {
        **headers, "x-correlation-id": str(uuid.uuid4()),
        "traceparent": f"00-{uuid.uuid4().hex}-{secrets.token_hex(8)}-01",
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    evidence = EVENTS.begin("POST", url, req.headers, body)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as response:
            response_headers = dict(response.headers.items())
            if "id" not in body:
                EVENTS.end(evidence, response.status, response_headers, None, encoding="empty")
                if response.status not in (200, 202, 204):
                    raise DemoError("The MCP notification was not accepted.")
                return {}, response_headers
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                encoding = "sse"
                total = 0
                for raw in response:
                    total += len(raw)
                    if total > 1024 * 1024:
                        raise DemoError("The MCP response exceeded the demonstration limit.")
                    line = raw.decode("utf-8").strip()
                    if line.startswith("data:"):
                        message = json.loads(line[5:])
                        if message.get("id") == body["id"]:
                            break
                else:
                    raise DemoError("The MCP stream ended without the expected response.")
            else:
                encoding = "json"
                message = json.loads(response.read(1024 * 1024))
            EVENTS.end(evidence, response.status, response_headers, message, encoding=encoding)
            if "error" in message:
                raise DemoError(f"MCP returned JSON-RPC error {message['error'].get('code')}.")
            if message.get("id") != body["id"] or not isinstance(message.get("result"), dict):
                raise DemoError("The MCP response did not match the request.")
            return message["result"], response_headers
    except urllib.error.HTTPError as error:
        with error:
            EVENTS.end(evidence, error.code, dict(error.headers.items()),
                       error.read(16384).decode("utf-8", errors="replace"), encoding="text")
            raise DemoError(f"MCP request failed: HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        EVENTS.end(evidence, None, {}, None, error="The MCP request failed or timed out.")
        raise DemoError("The MCP request failed or timed out.") from error


def retry_seconds(headers: dict[str, str]) -> int:
    retry = next((value for name, value in headers.items() if name.lower() == "retry-after"), None)
    if retry is None or not str(retry).isdigit():
        raise DemoError("The rate response did not provide a numeric Retry-After value.")
    wait = int(retry) + 1
    if wait > 65:
        raise DemoError("The retry period is not the configured short demonstration window.")
    return max(wait, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(
        "preflight", "preview", "deploy", "inspect", "configure",
        "smoke", "rate", "configure-mcp", "mcp", "telemetry", "activity", "show",
        "queries", "inventory", "verify", "monitoring", "reset-demo", "configure-telemetry", "configure-aliases",
        "configure-routing", "routing-weighted", "routing-priority", "cleanup-legacy-telemetry",
    ))
    parser.add_argument("--config", type=Path, default=ROOT / ".azure" / "config.json")
    args = parser.parse_args()
    try:
        demo = Demo(args.config)
        EVENTS.plan(args.action, routing=bool(demo.config.get("routingModels")))
        result = getattr(demo, args.action.replace("-", "_"))()
        return result if isinstance(result, int) else 0
    except (DemoError, KeyError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    finally:
        EVENTS.finish()


if __name__ == "__main__":
    raise SystemExit(main())
