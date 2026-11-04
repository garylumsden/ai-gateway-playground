import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gateway_demo as gateway


# Public responses observed on 4 November 2026. No account data or credentials.
LEARN_RESULT = {"results": [{
    "title": "What is Azure API Management?",
    "content": "# What is Azure API Management?\n**APPLIES TO: All API Management tiers**",
    "contentUrl": "https://learn.microsoft.com/azure/api-management/api-management-key-concepts",
}]}
PRICE_RESULT = {
    "BillingCurrency": "USD", "CustomerEntityId": "Default", "CustomerEntityType": "Retail",
    "Items": [{
        "currencyCode": "USD", "tierMinimumUnits": 100.0, "retailPrice": 0.035, "unitPrice": 0.035,
        "armRegionName": "swedencentral", "location": "SE Central",
        "effectiveStartDate": "2024-04-01T00:00:00Z",
        "meterId": "fec1b9de-9bd9-56f9-8a21-3cf997d5c678", "meterName": "Consumption Calls",
        "productId": "DZH318Z0BQCM", "skuId": "DZH318Z0BQCM/00GN", "productName": "API Management",
        "skuName": "Consumption", "serviceName": "API Management", "serviceId": "DZH318FZ20SD",
        "serviceFamily": "Developer Tools", "unitOfMeasure": "10K", "type": "Consumption",
        "isPrimaryMeterRegion": True, "armSkuName": "",
    }],
    "NextPageLink": None, "Count": 1,
}


def demo_instance():
    demo = gateway.Demo.__new__(gateway.Demo)
    demo.config = {"location": "swedencentral", "model": "owned-model"}
    demo.state = {
        "gatewayId": "/subscriptions/example/resourceGroups/owned/providers/Microsoft.ApiManagement/aigateways/owned",
        "hostname": "gateway.example.invalid",
        "mcpEndpoints": {
            "learn": "https://gateway.example.invalid/native/learn/mcp",
            "prices": "https://gateway.example.invalid/native/prices/mcp",
        },
        "mcpSources": {
            "learn": "Microsoft Learn public documentation",
            "prices": "Microsoft Azure Retail Prices public list prices",
        },
    }
    return demo


class NativeMcpConfigurationTests(unittest.TestCase):
    def test_configures_two_native_anonymous_backends_without_touching_models(self):
        demo = demo_instance()
        demo.state.update({"mcpEndpoint": "old-model-endpoint", "mcpChecks": {"old": True}})
        writes = {}

        def arm(path, method="GET", body=None, **kwargs):
            self.assertEqual(kwargs["version"], "2026-09-01-preview")
            self.assertEqual(method, "PUT")
            writes[path.rsplit("/", 1)[-1]] = body["properties"]
            return {"properties": {"gatewayEndpoints": [{
                "url": "https://gateway.example.invalid/native/" + path.rsplit("/", 1)[-1] + "/mcp",
            }]}}

        with patch.object(demo, "arm", side_effect=arm), patch.object(demo, "save"):
            demo.configure_mcp()

        self.assertEqual(set(writes), {"learn", "prices"})
        self.assertNotIn("mcpEndpoint", demo.state)
        self.assertNotIn("mcpChecks", demo.state)
        for properties in writes.values():
            self.assertTrue(properties["subscriptionRequired"])
            self.assertEqual(properties["failureMode"], "failClosed")
            self.assertEqual(properties["endpoints"][0]["credentials"], {"type": "none"})
        learn = writes["learn"]["endpoints"][0]
        self.assertEqual(learn["mcp"], {
            "url": "https://learn.microsoft.com/api/mcp", "transport": "streamableHttp",
        })
        self.assertEqual(learn["tools"], ["microsoft_docs_search", "microsoft_docs_fetch"])
        prices = writes["prices"]["endpoints"][0]
        spec = json.loads(base64.b64decode(prices["openApi"]["specSource"]["contentBase64"]))
        self.assertEqual(spec["servers"], [{"url": "https://prices.azure.com"}])
        operation = spec["paths"]["/api/retail/prices"]["get"]
        self.assertEqual(operation["operationId"], "get_azure_retail_prices")
        self.assertEqual(operation["parameters"][0]["schema"]["enum"], [
            "serviceName eq 'API Management' and armRegionName eq 'swedencentral' and skuName eq 'Consumption'",
        ])
        self.assertEqual(prices["tools"], ["get_azure_retail_prices"])

    def test_does_not_invent_gateway_endpoint_when_arm_omits_it(self):
        demo = demo_instance()
        with patch.object(demo, "arm", return_value={"properties": {}}), patch.object(demo, "save"):
            with self.assertRaises(gateway.DemoError):
                demo.configure_mcp()


class PublicMcpInvocationTests(unittest.TestCase):
    def rpc_responses(self, tool, result):
        return [
            ({"protocolVersion": "2025-03-26"}, {"mcp-session-id": "session"}),
            ({}, {}),
            ({"tools": [tool]}, {}),
            ({"content": [{"type": "text", "text": json.dumps(result)}]}, {}),
        ]

    def test_discovers_actual_names_and_returns_bounded_document_evidence(self):
        tool = {"name": "changed_prefix_microsoft_docs_search", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}},
        }}
        with patch("gateway_demo.mcp_rpc", side_effect=self.rpc_responses(tool, LEARN_RESULT)) as rpc:
            result = gateway.public_tool_call(
                "https://gateway.example.invalid/mcp", {"api-key": "gateway-secret"}, "learn", "swedencentral",
            )
        invocation = rpc.call_args_list[-1].args[2]["params"]
        self.assertEqual(invocation, {
            "name": "changed_prefix_microsoft_docs_search",
            "arguments": {"query": "Azure API Management overview"},
        })
        self.assertEqual(result["tool"], tool["name"])
        self.assertEqual(result["toolCount"], 1)
        self.assertEqual(result["result"]["documents"][0]["title"], "What is Azure API Management?")
        self.assertEqual(result["result"]["documents"][0]["url"], LEARN_RESULT["results"][0]["contentUrl"])

    def test_calls_only_the_filtered_prices_operation_and_keeps_units_and_tiers(self):
        tool = {"name": "retail_get_azure_retail_prices", "inputSchema": {
            "type": "object", "properties": {"$filter": {"type": "string"}}, "required": ["$filter"],
        }}
        with patch("gateway_demo.mcp_rpc", side_effect=self.rpc_responses(tool, PRICE_RESULT)) as rpc:
            result = gateway.public_tool_call(
                "https://gateway.example.invalid/mcp", {}, "prices", "swedencentral",
            )
        arguments = rpc.call_args_list[-1].args[2]["params"]["arguments"]
        self.assertEqual(arguments, {
            "$filter": "serviceName eq 'API Management' and armRegionName eq 'swedencentral' and skuName eq 'Consumption'",
        })
        self.assertEqual(result["result"]["items"][0]["retailPrice"], 0.035)
        self.assertEqual(result["result"]["items"][0]["unitOfMeasure"], "10K")
        self.assertEqual(result["result"]["items"][0]["tierMinimumUnits"], 100)
        self.assertEqual(result["result"]["pagesRetrieved"], 1)
        self.assertFalse(result["result"]["hasMorePages"])
        self.assertIn("public", result["result"]["priceBasis"])

    def test_reports_pagination_without_requesting_arbitrary_backend_url(self):
        payload = {**PRICE_RESULT, "NextPageLink": "https://prices.azure.com/api/retail/prices?$skip=1000"}
        tool = {"name": "get_azure_retail_prices", "inputSchema": {
            "type": "object", "properties": {"$filter": {"type": "string"}},
        }}
        with patch("gateway_demo.mcp_rpc", side_effect=self.rpc_responses(tool, payload)):
            result = gateway.public_tool_call("https://gateway.example.invalid/mcp", {}, "prices", "swedencentral")
        self.assertTrue(result["result"]["hasMorePages"])
        self.assertEqual(result["result"]["pagesRetrieved"], 1)

    def test_tool_error_and_invalid_public_payload_do_not_become_success(self):
        tool = {"name": "microsoft_docs_search", "inputSchema": {"properties": {"query": {"type": "string"}}}}
        for payload in ({"isError": True}, {"structuredContent": {"results": []}}, {
            "structuredContent": {"results": [{"title": "bad", "contentUrl": "javascript:alert(1)"}]},
        }):
            responses = self.rpc_responses(tool, LEARN_RESULT)
            responses[-1] = (payload, {})
            with self.subTest(payload=payload), patch("gateway_demo.mcp_rpc", side_effect=responses):
                with self.assertRaises(gateway.DemoError):
                    gateway.public_tool_call("https://gateway.example.invalid/mcp", {}, "learn", "swedencentral")


class McpEvidenceTests(unittest.TestCase):
    def test_both_backends_are_checked_and_partial_failure_is_saved(self):
        demo = demo_instance()
        demo.state["mcpChecks"] = {"learn": {"status": "passed", "result": "stale"}}
        results = [
            gateway.DemoError("Private response must not be persisted."),
            {"tool": "prices_get_azure_retail_prices", "toolCount": 1, "result": {"items": []}},
        ]
        with patch.object(demo, "save") as save, patch.object(demo, "runtime_key", return_value="secret"), \
                patch("gateway_demo.request", return_value=(401, {}, {})) as request, \
                patch("gateway_demo.public_tool_call", side_effect=results) as calls, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(gateway.DemoError):
                demo.mcp()
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(demo.state["mcpChecks"]["learn"]["status"], "failed")
        self.assertNotIn("Private response", str(demo.state))
        prices = demo.state["mcpChecks"]["prices"]
        self.assertEqual(prices["status"], "passed")
        self.assertEqual(prices["authentication"], [
            {"check": "missingCredential", "httpStatus": 401},
            {"check": "invalidCredential", "httpStatus": 401},
        ])
        self.assertTrue(save.called)
        self.assertNotIn("stale", str(demo.state))

    def test_missing_and_invalid_credentials_must_be_rejected_for_each_backend(self):
        demo = demo_instance()
        with patch.object(demo, "save"), patch.object(demo, "runtime_key", return_value="secret"), \
                patch("gateway_demo.request", return_value=(200, {}, {})), \
                patch("gateway_demo.public_tool_call") as invoke, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(gateway.DemoError):
                demo.mcp()
        invoke.assert_not_called()
        self.assertEqual([item["status"] for item in demo.state["mcpChecks"].values()], ["failed", "failed"])

    def test_non_json_authentication_failure_does_not_skip_the_second_source(self):
        demo = demo_instance()
        with patch.object(demo, "save"), patch.object(demo, "runtime_key", return_value="secret"), \
                patch("gateway_demo.request", side_effect=[
                    json.JSONDecodeError("Invalid gateway response", "<html>", 0),
                    (401, {}, {}), (403, {}, {}),
                ]), patch("gateway_demo.public_tool_call", return_value={
                    "tool": "get_azure_retail_prices", "toolCount": 1, "result": {"items": []},
                }), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(gateway.DemoError):
                demo.mcp()
        self.assertEqual(demo.state["mcpChecks"]["learn"]["status"], "failed")
        self.assertEqual(demo.state["mcpChecks"]["prices"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
