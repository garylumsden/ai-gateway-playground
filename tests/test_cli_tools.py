import contextlib
import io
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gateway_demo import Demo, DemoError, MODEL_ALIAS
from redaction import redact


def capture_context():
    config = {
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
    }
    state = {
        "hostname": "private-gateway.example.invalid",
        "gatewayName": "private-gateway",
        "applicationInsightsId": "/subscriptions/example/providers/Microsoft.Insights/components/private-insights",
        "foundryName": "private-foundry", "logWorkspaceName": "private-workspace",
    }
    return config, state


class OutputRedactionTests(unittest.TestCase):
    def test_private_context_is_redacted_before_rendering(self):
        config, state = capture_context()
        text = "\n".join([
            config["subscriptionId"], state["hostname"], state["gatewayName"],
            state["applicationInsightsId"], state["foundryName"], state["logWorkspaceName"],
        ])
        result = redact(text, config, state)
        for private in (
            config["subscriptionId"], state["hostname"], state["gatewayName"],
            "private-insights", state["foundryName"], state["logWorkspaceName"],
        ):
            self.assertNotIn(private, result)
        self.assertIn("<gateway-host>", result)
        self.assertIn("<subscription-id>", result)

    def test_credentials_and_connection_strings_are_redacted(self):
        config, state = capture_context()
        secret = "a" * 64
        result = redact(f"Bearer {secret}\n{secret}\nInstrumentationKey=test-value;IngestionEndpoint=private", config, state)
        self.assertNotIn(secret, result)
        self.assertNotIn("test-value", result)

    def test_operator_profile_path_is_redacted(self):
        config, state = capture_context()
        result = redact(r"C:\Users\ExampleOperator\source\repo\script.py", config, state)
        self.assertNotIn("ExampleOperator", result)
        self.assertIn("<operator>", result)

class PresenterMapTests(unittest.TestCase):
    def demo(self):
        demo = Demo.__new__(Demo)
        demo.prefix = "/subscriptions/example"
        demo.config = {
            "resourceGroupName": "rg-new-demo",
        }
        demo.state = {
            "hostname": "gateway.example.invalid", "gatewayName": "gateway", "sku": "Consumption",
            "applicationInsightsId": "/subscriptions/example/resourceGroups/rg-new-demo/providers/Microsoft.Insights/components/new-insights",
            "modelEndpoints": {MODEL_ALIAS: "https://gateway.example.invalid/models/chat/completions"},
            "mcpEndpoint": "https://gateway.example.invalid/default/toolservers/model-tools/mcp",
            "foundryId": "/subscriptions/example/resourceGroups/rg-new-demo/providers/Microsoft.CognitiveServices/accounts/playground-models",
            "logWorkspaceId": "/subscriptions/example/resourceGroups/rg-new-demo/providers/Microsoft.OperationalInsights/workspaces/playground-logs",
        }
        return demo

    def test_browser_links_and_api_addresses_are_separate(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.demo().show()
        text = output.getvalue()
        self.assertLess(text.index("OPEN THESE LINKS IN A BROWSER"), text.index("API ADDRESSES"))
        self.assertIn("Foundry models and Log Analytics belong to this playground.", text)
        self.assertIn("Workspace Logs:", text)
        self.assertNotIn("#resource//", text)
        self.assertIn("AI Gateway SKU: Consumption", text)

    def test_missing_local_state_has_an_actionable_error(self):
        demo = self.demo()
        demo.state = {}
        with self.assertRaisesRegex(DemoError, "Restore the approved operator state"):
            demo.show()

    def test_queries_are_ready_to_paste_and_component_scoped(self):
        demo = self.demo()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            demo.queries()
        text = output.getvalue()
        self.assertIn("PREPARE PRIVATELY", text)
        self.assertEqual(text.count(demo.state["applicationInsightsId"]), 2)
        self.assertIn("NATIVE ACTIVITY", text)
        self.assertIn("NATIVE TOKEN REPORTING", text)
        self.assertNotIn("<application-insights-resource-id>", text)

    def test_native_queries_use_server_spans_and_the_real_metric(self):
        demo = self.demo()
        demo.state["nativeOtlp"] = {"configured": True}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            demo.queries()
        text = output.getvalue()
        self.assertIn("OTelSpans", text)
        self.assertIn("Kind =~ 'Server'", text)
        self.assertIn("PROMQL", text)
        self.assertIn("azure.ai_gateway.client.token.usage", text)
        self.assertNotIn("gen_ai.usage.input_tokens", text)
        self.assertEqual(text.lower().count(demo.state["applicationInsightsId"].lower()), 2)

    def test_cleanup_cannot_cross_the_gateway_workspace_boundary(self):
        demo = self.demo()
        demo.state["gatewayId"] = "/subscriptions/example/resourceGroups/rg-new-demo/providers/Microsoft.ApiManagement/aigateways/demo"
        for path in (
            "/subscriptions/example/resourceGroups/rg-unrelated/providers/Microsoft.CognitiveServices/accounts/unrelated",
            demo.gateway_id,
            demo.gateway_id + "/workspaces/another/aliases/alias",
            demo.gateway_id + "/workspaces/default-extra/aliases/alias",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(DemoError, "restricted"):
                demo.delete_owned_gateway_asset(path)

    def test_invalid_routing_mode_does_not_touch_azure(self):
        with self.assertRaisesRegex(DemoError, "Routing mode"):
            self.demo().routing("unexpected")

    def test_cleanup_rejects_an_unrelated_gateway_id_in_local_state(self):
        demo = self.demo()
        demo.state["gatewayId"] = "/subscriptions/example/resourceGroups/rg-unrelated/providers/Microsoft.ApiManagement/aigateways/demo"
        with self.assertRaisesRegex(DemoError, "restricted"):
            demo.delete_owned_gateway_asset(demo.gateway_id + "/workspaces/default/aliases/alias")

    def test_invalid_routing_deployment_shapes_do_not_touch_azure(self):
        for names in ([], ["one"], ["one", "one", "two"], ["one", {}, "two"], ["one", "../two", "three"]):
            demo = self.demo()
            demo.config["routingModels"] = names
            with self.subTest(names=names), self.assertRaisesRegex(DemoError, "three distinct"):
                demo.configure_routing()

if __name__ == "__main__":
    unittest.main()
