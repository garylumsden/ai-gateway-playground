"""Check regional model quota and configure the provisioned AI Gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Generic, TypeVar

from pydantic import BaseModel, Json, ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from gateway_demo import Demo, DemoError, azd, request

COGNITIVE_API = "2025-06-01"
T = TypeVar("T")


class ModelDeployment(BaseModel):
    name: str
    model: str
    version: str
    sku: str
    capacity: int


class GatewaySettings(BaseModel):
    subscriptionId: str
    location: str
    resourceGroupName: str
    deploymentName: str
    modelDeployments: list[ModelDeployment]
    model: str
    routingModels: list[str]
    allowNativeTokenGap: bool


class Environment(BaseModel):
    AZURE_ENV_NAME: str
    AZURE_SUBSCRIPTION_ID: str
    AZURE_LOCATION: str
    foundryId: str = ""


class ProvisionedEnvironment(Environment):
    AZURE_TENANT_ID: str
    AZURE_RESOURCE_GROUP: str
    AZURE_MODEL_DEPLOYMENTS: Json[list[ModelDeployment]]
    gatewayId: str
    gatewayName: str
    hostname: str
    applicationInsightsId: str
    foundryId: str
    foundryName: str
    logWorkspaceId: str
    logWorkspaceName: str
    modelAccessId: str


class Page(BaseModel, Generic[T]):
    value: list[T]
    nextLink: str | None = None


class ModelSku(BaseModel):
    name: str
    usageName: str


class CatalogModel(BaseModel):
    name: str
    version: str
    skus: list[ModelSku]


class ModelEntry(BaseModel):
    model: CatalogModel


class UsageName(BaseModel):
    value: str


class Usage(BaseModel):
    name: UsageName
    currentValue: float
    limit: float


class ResourceType(BaseModel):
    resourceType: str
    locations: list[str] = []


class ResourceProvider(BaseModel):
    resourceTypes: list[ResourceType]


class DeployedSku(BaseModel):
    name: str
    capacity: int


class DeployedModel(BaseModel):
    name: str


class DeploymentProperties(BaseModel):
    model: DeployedModel


class OwnedDeployment(BaseModel):
    sku: DeployedSku
    properties: DeploymentProperties


class FoundryAccount(BaseModel):
    location: str


def read_arm(path: str, token: str, *, missing_ok: bool = False) -> dict | None:
    url = path if path.startswith("https://") else "https://management.azure.com" + path
    status, _, payload = request("GET", url, {"Authorization": "Bearer " + token})
    if status == 404 and missing_ok:
        return None
    if status != 200:
        raise DemoError(f"Azure availability check failed: HTTP {status}. Check the selected subscription and location.")
    return payload


def read_collection(path: str, token: str, item_type: type[T]) -> list[T]:
    result = []
    while path:
        page = Page[item_type].model_validate(read_arm(path, token))
        result.extend(page.value)
        path = page.nextLink
    return result


def owned_capacity(environment: Environment, token: str) -> dict[str, int]:
    if not environment.foundryId:
        return {}
    account = read_arm(environment.foundryId + "?api-version=" + COGNITIVE_API, token, missing_ok=True)
    if not account:
        return {}
    foundry = FoundryAccount.model_validate(account)
    if foundry.location.lower() != environment.AZURE_LOCATION.lower():
        return {}
    deployments = read_collection(
        environment.foundryId + "/deployments?api-version=" + COGNITIVE_API, token, OwnedDeployment,
    )
    capacity = {}
    for deployment in deployments:
        key = f"OpenAI.{deployment.sku.name}.{deployment.properties.model.name}"
        capacity[key] = capacity.get(key, 0) + deployment.sku.capacity
    return capacity


def check_models(environment: Environment, settings: GatewaySettings, token: str) -> None:
    prefix = (
        f"/subscriptions/{environment.AZURE_SUBSCRIPTION_ID}/providers/"
        f"Microsoft.CognitiveServices/locations/{environment.AZURE_LOCATION}"
    )
    entries = read_collection(prefix + "/models?api-version=" + COGNITIVE_API, token, ModelEntry)
    usages = read_collection(prefix + "/usages?api-version=" + COGNITIVE_API, token, Usage)
    quotas = {usage.name.value: usage for usage in usages}
    allocated = owned_capacity(environment, token)
    for deployment in settings.modelDeployments:
        skus = [
            sku for entry in entries for sku in entry.model.skus
            if (entry.model.name, entry.model.version, sku.name)
            == (deployment.model, deployment.version, deployment.sku)
        ]
        if not skus:
            raise DemoError(
                f"{deployment.model} {deployment.version} / {deployment.sku} is unavailable in the selected location."
            )
        quota = quotas[skus[0].usageName]
        available = quota.limit - quota.currentValue + allocated.get(quota.name.value, 0)
        if available < deployment.capacity:
            raise DemoError(
                f"{deployment.model} needs {deployment.capacity} capacity units; {available:g} are available. "
                "Choose another location or request quota."
            )
        print(f"{deployment.model}: {available:g} capacity units available; {deployment.capacity} required.")


def prepare(environment: Environment) -> None:
    settings = GatewaySettings.model_validate_json((ROOT / "config.example.json").read_text(encoding="utf-8"))
    token = azd(["auth", "token", "-e", environment.AZURE_ENV_NAME])["token"]
    path = (
        f"/subscriptions/{environment.AZURE_SUBSCRIPTION_ID}/providers/"
        "Microsoft.ApiManagement?api-version=2021-04-01"
    )
    provider = ResourceProvider.model_validate(read_arm(path, token))
    gateway = next(item for item in provider.resourceTypes if item.resourceType == "aigateways")
    locations = {location.replace(" ", "").lower() for location in gateway.locations}
    if environment.AZURE_LOCATION.lower() not in locations:
        raise DemoError("AI Gateway is unavailable in the selected location. Choose a supported location.")
    check_models(environment, settings, token)
    print("Regional model availability and quota checks passed. Model deployments belong to this environment.")


def configure(environment: ProvisionedEnvironment) -> None:
    local = ROOT / ".azure" / environment.AZURE_ENV_NAME
    settings = GatewaySettings.model_validate_json((ROOT / "config.example.json").read_text(encoding="utf-8"))
    settings.subscriptionId = environment.AZURE_SUBSCRIPTION_ID
    settings.location = environment.AZURE_LOCATION
    settings.resourceGroupName = environment.AZURE_RESOURCE_GROUP
    settings.deploymentName = environment.AZURE_ENV_NAME
    settings.modelDeployments = environment.AZURE_MODEL_DEPLOYMENTS
    config = settings.model_dump()
    config.update({"azdEnvironment": environment.AZURE_ENV_NAME, "tenantId": environment.AZURE_TENANT_ID})
    path = local / "gateway-config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    gateway = Demo(path)
    if gateway.state.get("gatewayId") != environment.gatewayId:
        gateway.state = {}
    for name in (
        "gatewayId", "gatewayName", "hostname", "applicationInsightsId", "foundryId",
        "foundryName", "logWorkspaceId", "logWorkspaceName", "modelAccessId",
    ):
        gateway.state[name] = getattr(environment, name)
    gateway.state["deployed"] = True
    gateway.save()
    gateway.configure()
    gateway.configure_mcp()
    gateway.configure_routing()
    print("Gateway setup finished. Run pwsh .\\scripts\\start-presenter.ps1 on Windows to open the application.")


def main() -> int:
    try:
        values = azd(["env", "get-values", "-e", os.environ["AZURE_ENV_NAME"]])
        if sys.argv[1] == "prepare":
            prepare(Environment.model_validate(values))
        else:
            configure(ProvisionedEnvironment.model_validate(values))
        return 0
    except (DemoError, ValidationError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
