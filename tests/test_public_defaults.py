import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gateway_demo import LIMITED_ALIAS, LIMIT_PROMPT, MODEL_ALIAS, MODEL_PROMPTS, ROUTING_ALIAS, ROUTING_PROMPT, Demo, DemoError
from presenter import ENTROPY, TELEMETRY_ROLE, deployed_resources


class PublicDefaultsTests(unittest.TestCase):
    def test_example_configuration_uses_placeholders_for_private_resources(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        for value in (config["subscriptionId"], config["location"]):
            self.assertRegex(value, r"^<[a-z-]+>$")
        self.assertEqual(config["resourceGroupName"], "rg-ai-gateway-playground")
        self.assertEqual(config["deploymentName"], "ai-gateway-playground")
        self.assertEqual(set(config), {
            "subscriptionId", "location", "resourceGroupName", "deploymentName",
            "modelDeployments", "model", "routingModels", "allowNativeTokenGap",
        })
        self.assertEqual({item["name"] for item in config["modelDeployments"]}, set(config["routingModels"]))
        self.assertFalse(config["allowNativeTokenGap"])

    def test_alias_names_and_ui_routing_link_agree(self):
        self.assertEqual((MODEL_ALIAS, LIMITED_ALIAS, ROUTING_ALIAS), (
            "playground-model", "playground-limited", "playground-routing",
        ))
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('path:"/catalog/models/' + ROUTING_ALIAS + '"', script)

    def test_prompts_use_generic_project_tasks(self):
        self.assertIn("support-ticket dataset", MODEL_PROMPTS[0])
        self.assertIn("software project team", MODEL_PROMPTS[1])
        self.assertIn("project report", MODEL_PROMPTS[2])
        for prompt in (*MODEL_PROMPTS, ROUTING_PROMPT, LIMIT_PROMPT):
            self.assertNotIn("https://", prompt)
            self.assertNotIn("@", prompt)
            self.assertNotIn("/subscriptions/", prompt)

    def test_local_identity_labels_use_the_public_project_name(self):
        self.assertEqual(TELEMETRY_ROLE, "ai-gateway-playground-presenter")
        self.assertTrue(ENTROPY.startswith(b"ai-gateway-playground-presenter-"))

    def test_self_contained_configuration_does_not_need_external_resources(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config.update({"subscriptionId": "00000000-0000-0000-0000-000000000000", "location": "example-location"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            demo = Demo(path)
            self.assertEqual(set(demo.parameters()), {"location", "resourceGroupName", "modelDeployments"})
            config["modelDeployments"][1]["name"] = config["modelDeployments"][0]["name"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(DemoError, "distinct"):
                Demo(path)

    def test_resource_links_only_use_owned_playground_resources(self):
        prefix = "/subscriptions/example/resourceGroups/rg-playground"
        demo = SimpleNamespace(
            config={"resourceGroupName": "rg-playground"}, prefix="/subscriptions/example",
            gateway_id=prefix + "/providers/Microsoft.ApiManagement/aigateways/playground",
            foundry_id=prefix + "/providers/Microsoft.CognitiveServices/accounts/playground",
            state={
                "gatewayName": "playground", "applicationInsightsId": prefix + "/providers/Microsoft.Insights/components/playground",
                "foundryName": "playground", "logWorkspaceName": "playground",
                "logWorkspaceId": prefix + "/providers/Microsoft.OperationalInsights/workspaces/playground",
            },
        )
        links = deployed_resources(demo)
        self.assertEqual(len(links), 7)
        for link in links:
            if link["id"] != "gatewayPortal":
                self.assertIn(prefix, link["href"])

    def test_ownership_inventory_contains_only_playground_resources(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        demo = object.__new__(Demo)
        demo.config = config
        prefix = "/subscriptions/example/resourceGroups/" + config["resourceGroupName"]
        demo.state = {
            "gatewayId": prefix + "/providers/Microsoft.ApiManagement/aigateways/playground",
            "foundryId": prefix + "/providers/Microsoft.CognitiveServices/accounts/playground",
            "applicationInsightsId": prefix + "/providers/Microsoft.Insights/components/playground",
            "logWorkspaceId": prefix + "/providers/Microsoft.OperationalInsights/workspaces/playground",
            "modelAccessId": prefix + "/providers/Microsoft.Authorization/roleAssignments/example",
        }
        demo.prefix = "/subscriptions/example"
        with patch("gateway_demo.Path.write_text") as write:
            demo.inventory()
        inventory = json.loads(write.call_args.args[0])
        self.assertEqual(inventory["createdResourceGroup"], config["resourceGroupName"])
        self.assertTrue(inventory["cleanupRequiresApproval"])
        for identifier in inventory["ownedResources"] + inventory["modelDeployments"]:
            self.assertTrue(identifier.startswith(prefix + "/"))

    def test_setup_stage_plan_has_only_gateway_experiments(self):
        from demo_events import ACTION_STAGES
        self.assertEqual(ACTION_STAGES["verify"], (
            "model-access", "request-limits", "ai-gateway-mcp", "native-activity",
            "routing-priority", "routing-weighted", "native-tokens",
        ))


if __name__ == "__main__":
    unittest.main()
