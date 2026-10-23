import base64
import json
import sys
from pathlib import Path
import unittest
from copy import deepcopy
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gateway_demo import LIMIT_PROMPT, MODEL_PROMPTS, ROUTING_PROMPT, Demo, DemoError, NoRedirect, model_tool_call, tool_model_result, native_cost_totals, native_token_totals, require_runtime_url, retry_seconds, runtime_endpoint, secret_values


class ModelToolResultTests(unittest.TestCase):
    def response(self):
        return {
            "model": "example-model", "choices": [{"message": {"content": "Assign task owners and a due date."}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22},
        }

    def test_accepts_structured_content_and_json_text(self):
        import json
        expected = Demo.model_result(200, self.response())
        self.assertEqual(tool_model_result({"structuredContent": self.response()}), expected)
        self.assertEqual(tool_model_result({"content": [{"type": "text", "text": "\ufeff" + json.dumps(self.response())}]}), expected)

    def test_tool_error_is_not_a_successful_model_answer(self):
        with self.assertRaisesRegex(DemoError, "model tool returned an error"):
            tool_model_result({"isError": True, "content": [{"type": "text", "text": "Tool invocation failed."}]})

    def test_html_error_page_is_not_model_json(self):
        with self.assertRaisesRegex(DemoError, "valid JSON"):
            tool_model_result({"isError": False, "content": [{"type": "text", "text": "<html>Server error</html>"}]})

    def test_valid_model_answer_and_usage_are_required(self):
        for result in (
            {"isError": True, "content": [{"type": "text", "text": "HTTP 401"}]},
            {"content": [{"type": "text", "text": "unexpected text"}]},
            {"structuredContent": {"choices": [], "usage": {"total_tokens": 1}}},
        ):
            with self.subTest(result=result), self.assertRaises(DemoError):
                tool_model_result(result)

    def test_model_tool_protocol_uses_the_selected_owned_model(self):
        responses = [
            ({"protocolVersion": "2025-03-26"}, {}),
            ({}, {}),
            ({"tools": [{"name": "model_summarize_text"}]}, {}),
            ({"structuredContent": self.response()}, {}),
        ]
        with patch("gateway_demo.mcp_rpc", side_effect=responses) as rpc:
            result = model_tool_call("https://gateway.example.invalid/mcp", {}, "example-model")
        body = rpc.call_args_list[-1].args[2]
        self.assertEqual(body["params"]["name"], "model_summarize_text")
        self.assertEqual(body["params"]["arguments"]["model"], "example-model")
        self.assertEqual(body["params"]["arguments"]["messages"][0]["role"], "user")
        self.assertTrue(result["answerPresent"])
        self.assertEqual(result["toolCount"], 1)
        self.assertEqual(result["totalTokens"], 22)

    def test_mcp_configuration_points_to_owned_foundry_and_managed_identity(self):
        demo = object.__new__(Demo)
        demo.config = {"model": "example-model"}
        demo.state = {
            "gatewayId": "/subscriptions/example/resourceGroups/playground/providers/Microsoft.ApiManagement/aigateways/playground",
            "hostname": "gateway.example.invalid",
            "foundryId": "/subscriptions/example/resourceGroups/playground/providers/Microsoft.CognitiveServices/accounts/playground",
        }
        def arm(path, method="GET", body=None, **kwargs):
            if path == demo.foundry_id:
                return {"properties": {"endpoint": "https://playground.cognitiveservices.azure.com/"}}
            self.assertEqual(method, "PUT")
            return {"properties": {"gatewayEndpoints": [{"url": "https://gateway.example.invalid/default/toolservers/model-tools/mcp"}]}}
        with patch.object(demo, "arm", side_effect=arm) as calls, patch.object(demo, "save"):
            demo.configure_mcp()
        properties = calls.call_args.args[2]["properties"]
        backend = properties["endpoints"][0]
        self.assertEqual(backend["credentials"], {
            "type": "managedIdentity", "managedIdentity": {"resource": "https://cognitiveservices.azure.com"},
        })
        self.assertEqual(backend["tools"], ["summarize_text"])
        spec = json.loads(base64.b64decode(backend["openApi"]["specSource"]["contentBase64"]))
        self.assertEqual(spec["servers"][0]["url"], "https://playground.cognitiveservices.azure.com")
        operation = spec["paths"]["/openai/v1/chat/completions"]["post"]
        self.assertEqual(operation["operationId"], "summarize_text")
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["model"]["enum"], ["example-model"])


class NativeTokenTests(unittest.TestCase):
    def payload(self, values):
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"gen_ai.token.type": token_type}, "value": [100, str(value)]}
            for token_type, value in values
        ]}}

    def test_reads_native_counters_without_double_counting_details(self):
        result = native_token_totals(self.payload([
            ("prompt_tokens", 13), ("completion_tokens", 4), ("total_tokens", 17),
            ("prompt_tokens_details.cached_tokens", 8), ("completion_tokens_details.reasoning_tokens", 2),
        ]))
        self.assertEqual(result["InputTokens"], 13)
        self.assertEqual(result["OutputTokens"], 4)
        self.assertEqual(result["Source"], "native-otlp")

    def test_sums_independent_native_model_series(self):
        result = native_token_totals(self.payload([
            ("input_tokens", 5), ("prompt_tokens", 8), ("output_tokens", 3), ("completion_tokens", 4),
        ]))
        self.assertEqual(result["InputTokens"], 13)
        self.assertEqual(result["OutputTokens"], 7)

    def test_does_not_turn_missing_usage_into_a_positive_value(self):
        result = native_token_totals(self.payload([]))
        self.assertEqual(result["Records"], 0)
        self.assertEqual(result["InputTokens"], 0)
        self.assertEqual(result["OutputTokens"], 0)

    def test_rejects_failed_and_invalid_metric_results(self):
        for payload in ({}, {"status": "error"}, {"status": "success", "data": {"resultType": "matrix", "result": []}}):
            with self.subTest(payload=payload), self.assertRaises(DemoError):
                native_token_totals(payload)
        for value in ("NaN", "Infinity", -1, "invalid"):
            with self.subTest(value=value), self.assertRaises(DemoError):
                native_token_totals(self.payload([("prompt_tokens", value)]))


class NativeCostTests(unittest.TestCase):
    def series(self, token_type, value, **labels):
        return {"metric": {
            "gen_ai.request.model": "example-alias", "gen_ai.response.model": "example-model",
            "azure.ai_gateway.currency": "usd", **labels,
            **({"gen_ai.token.type": token_type} if token_type is not None else {}),
        }, "value": [100, str(value)]}

    def payload(self, *series):
        return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}

    def test_total_takes_precedence_over_overlapping_components_and_summary(self):
        result = native_cost_totals(self.payload(
            self.series("prompt_tokens", 0.1), self.series("completion_tokens", 0.2),
            self.series("total_tokens", 0.3), self.series(None, 0.3),
            self.series("prompt_tokens_details.cached_tokens", 0.04),
            self.series("completion_tokens_details.reasoning_tokens", 0.05),
        ))
        self.assertTrue(result["Available"])
        self.assertAlmostEqual(result["Currencies"][0]["TotalCost"], 0.3)
        self.assertAlmostEqual(result["Models"][0]["InputCost"], 0.1)
        self.assertTrue(result["Currencies"][0]["Complete"])
        self.assertEqual(result["Currencies"][0]["Currency"], "USD")
        self.assertEqual(result["Aggregation"], "latest-cumulative")

    def test_base_components_are_used_only_when_total_is_absent(self):
        result = native_cost_totals(self.payload(
            self.series("input_tokens", 0.12), self.series("output_tokens", 0.08),
            self.series("output_tokens_details.reasoning_tokens", 0.02),
        ))
        self.assertAlmostEqual(result["Currencies"][0]["TotalCost"], 0.2)

    def test_currencies_remain_separate_and_independent_counter_dimensions_sum(self):
        result = native_cost_totals(self.payload(
            self.series("total_tokens", 0.2, **{"azure.ai_gateway.api_key_id": "first"}),
            self.series("total_tokens", 0.4, **{"azure.ai_gateway.api_key_id": "second"}),
            self.series("total_tokens", 0.7, **{"azure.ai_gateway.currency": "eur"}),
        ))
        totals = {item["Currency"]: item["TotalCost"] for item in result["Currencies"]}
        self.assertAlmostEqual(totals["USD"], 0.6)
        self.assertAlmostEqual(totals["EUR"], 0.7)
        self.assertEqual(len(result["Models"]), 2)
        self.assertNotIn("api_key_id", str(result))

    def test_missing_prices_mark_the_result_incomplete(self):
        result = native_cost_totals(self.payload(self.series(
            "total_tokens", 0, **{"azure.ai_gateway.price_missing": "true"},
        )))
        self.assertTrue(result["MissingPrices"])
        self.assertFalse(result["Currencies"][0]["Complete"])
        self.assertTrue(result["Models"][0]["MissingPrice"])

    def test_missing_data_is_unavailable_instead_of_zero_cost(self):
        for payload in (self.payload(), self.payload(self.series(None, 1)), self.payload(self.series("prompt_tokens_details.cached_tokens", 0.2))):
            with self.subTest(payload=payload):
                result = native_cost_totals(payload)
                self.assertFalse(result["Available"])
                self.assertEqual(result["Currencies"], [])
                self.assertTrue(result["Reason"])

    def test_rejects_invalid_amounts_currency_and_response_shapes(self):
        for value in ("NaN", "Infinity", -1, "invalid"):
            with self.subTest(value=value), self.assertRaises(DemoError):
                native_cost_totals(self.payload(self.series("total_tokens", value)))
        for currency in ("", "US", None):
            with self.subTest(currency=currency), self.assertRaises(DemoError):
                native_cost_totals(self.payload(self.series("total_tokens", 0.1, **{"azure.ai_gateway.currency": currency})))
        for payload in ({}, {"status": "error"}, self.payload("invalid")):
            with self.subTest(payload=payload), self.assertRaises(DemoError):
                native_cost_totals(payload)


class RuntimeUrlTests(unittest.TestCase):
    def test_accepts_exact_https_gateway(self):
        url = "https://gateway.example.invalid/models/responses"
        self.assertEqual(require_runtime_url(url, "gateway.example.invalid"), url)

    def test_rejects_other_host_protocol_credentials_and_port(self):
        for url in (
            "http://gateway.example.invalid/responses",
            "https://other.example.invalid/responses",
            "https://gateway.example.invalid.attacker.invalid/responses",
            "https://name:password@gateway.example.invalid/responses",
            "https://gateway.example.invalid:444/responses",
            "https://gateway.example.invalid/responses#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(DemoError):
                require_runtime_url(url, "gateway.example.invalid")

    def test_uses_returned_endpoint_instead_of_constructing_one(self):
        url = "https://gateway.example.invalid/actual/path/responses"
        resource = {"properties": {"gatewayEndpoints": [{"url": url}]}}
        self.assertEqual(runtime_endpoint(resource, "responses", "gateway.example.invalid"), url)

    def test_missing_endpoint_is_explicit_failure(self):
        with self.assertRaises(DemoError):
            runtime_endpoint({"properties": {}}, "responses", "gateway.example.invalid")

    def test_cross_host_endpoint_is_rejected(self):
        with self.assertRaises(DemoError):
            runtime_endpoint(
                {"properties": {"gatewayEndpoints": [{"url": "https://other.example.invalid/responses"}]}},
                "responses", "gateway.example.invalid",
            )

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.invalid"))

    def test_documented_route_uses_service_returned_relative_endpoint(self):
        resource = {"properties": {"supportedEndpoints": ["/openai/v1/chat/completions"]}}
        self.assertEqual(
            runtime_endpoint(resource, "chat/completions", "gateway.example.invalid", "/default/models"),
            "https://gateway.example.invalid/default/models/openai/v1/chat/completions",
        )

    def test_unreturned_operation_is_not_invented(self):
        with self.assertRaises(DemoError):
            runtime_endpoint(
                {"properties": {"supportedEndpoints": ["/openai/v1/chat/completions"]}},
                "responses", "gateway.example.invalid", "/default/models",
            )

    def test_relative_endpoint_cannot_traverse(self):
        with self.assertRaises(DemoError):
            runtime_endpoint(
                {"properties": {"supportedEndpoints": ["/../responses"]}},
                "responses", "gateway.example.invalid", "/default/models",
            )


class ResetDemoTests(unittest.TestCase):
    def demo(self):
        demo = object.__new__(Demo)
        demo.state = {
            "routingMode": "priority", "modelChecks": [{"httpStatus": 200}],
            "routingChecks": {"routing-priority": {"requests": 10}},
            "verified": True, "activityChecks": {"Requests": 12},
            "nativeOtlp": {"configured": True},
            "applicationInsightsId": "/components/retained-insights",
        }
        return demo

    def test_reset_clears_only_check_state_after_restoring_weights(self):
        demo = self.demo()
        def apply(mode):
            self.assertEqual(mode, "weighted")
            self.assertIn("modelChecks", demo.state)
            demo.state["routingMode"] = mode
        with patch.object(demo, "apply_routing_mode", side_effect=apply) as restore, patch.object(demo, "save"), patch("gateway_demo.time.sleep") as wait:
            demo.reset_demo()
        restore.assert_called_once_with("weighted")
        wait.assert_called_once_with(30)
        for key in ("modelChecks", "routingChecks", "verified", "activityChecks"):
            self.assertNotIn(key, demo.state)
        self.assertEqual(demo.state["routingMode"], "weighted")
        self.assertEqual(demo.state["nativeOtlp"], {"configured": True})
        self.assertEqual(demo.state["applicationInsightsId"], "/components/retained-insights")
        self.assertIn("presenterResetAt", demo.state)

    def test_failed_restore_does_not_clear_results(self):
        demo = self.demo()
        before = deepcopy(demo.state)
        with patch.object(demo, "apply_routing_mode", side_effect=DemoError("Restore failed.")), patch.object(demo, "save") as save:
            with self.assertRaisesRegex(DemoError, "Restore failed"):
                demo.reset_demo()
        self.assertEqual(demo.state, before)
        save.assert_not_called()


class ModelResultTests(unittest.TestCase):
    def test_prepared_prompts_reach_both_model_api_formats(self):
        self.assertEqual(len(set(MODEL_PROMPTS)), 3)
        self.assertTrue(all(len(prompt) > 150 for prompt in MODEL_PROMPTS))
        for prompt in (*MODEL_PROMPTS, ROUTING_PROMPT, LIMIT_PROMPT):
            self.assertNotRegex(prompt.lower(), r"\b(fictional|synthetic)\b")
        demo = object.__new__(Demo)
        for operation in ("chat/completions", "responses"):
            demo.state = {
                "hostname": "gateway.example.invalid",
                "modelEndpoints": {"example-alias": "https://gateway.example.invalid/default/models/openai/v1/" + operation},
            }
            with self.subTest(operation=operation), patch("gateway_demo.request", return_value=(200, {}, {})) as send:
                demo.model_request(
                    "example-alias", "not-a-real-gateway-key", key_header="api-key",
                    prompt=ROUTING_PROMPT, max_output_tokens=64,
                )
                payload = send.call_args.args[3]
                self.assertEqual(payload["model"], "example-alias")
                self.assertEqual(
                    payload["messages"][0]["content"] if operation == "chat/completions" else payload["input"],
                    ROUTING_PROMPT,
                )
                self.assertEqual(payload.get("max_completion_tokens", payload.get("max_output_tokens")), 64)

    def response(self, text="READY", total=8):
        return {
            "model": "demo-model",
            "output": [{"content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": total},
        }

    def test_success_requires_real_answer_and_nonzero_tokens(self):
        result = Demo.model_result(200, self.response())
        self.assertTrue(result["answerPresent"])
        self.assertEqual(result["totalTokens"], 8)
        self.assertNotIn("output", result)
        self.assertNotIn("text", result)

    def test_health_success_does_not_count_as_model_success(self):
        with self.assertRaises(DemoError):
            Demo.model_result(200, {"status": "ok"})

    def test_blank_output_is_failure(self):
        with self.assertRaises(DemoError):
            Demo.model_result(200, self.response(" "))

    def test_missing_or_zero_usage_is_failure(self):
        for total in (None, 0, "8", True):
            with self.subTest(total=total), self.assertRaises(DemoError):
                Demo.model_result(200, self.response(total=total))

    def test_status_is_not_hidden_by_response_shape(self):
        for status in (202, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(DemoError):
                Demo.model_result(status, self.response())

    def test_string_gateway_errors_do_not_raise_an_unhandled_exception(self):
        with self.assertRaisesRegex(DemoError, "HTTP 503"):
            Demo.model_result(503, {"error": "Service unavailable"})

    def test_chat_completions_shape_is_checked(self):
        result = Demo.model_result(200, {
            "model": "demo-model",
            "choices": [{"message": {"content": "READY"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        })
        self.assertEqual(result["inputTokens"], 5)
        self.assertEqual(result["outputTokens"], 3)


class RateAndSecretTests(unittest.TestCase):
    def test_retry_header_is_case_insensitive_and_bounded(self):
        self.assertEqual(retry_seconds({"Retry-After": "30"}), 31)
        self.assertEqual(retry_seconds({"retry-after": "0"}), 1)
        for headers in ({}, {"Retry-After": "invalid"}, {"Retry-After": "90"}):
            with self.subTest(headers=headers), self.assertRaises(DemoError):
                retry_seconds(headers)

    def test_secret_header_dictionaries_and_lists_are_detected(self):
        values = secret_values({"properties": {
            "primaryKey": "test-primary",
            "applicationInsights": {"connectionString": "test-connection"},
            "endpoints": [{"credentials": {"headers": {"x-key": ["test-header"]}}}],
        }})
        self.assertEqual(set(values), {"test-primary", "test-connection", "test-header"})


if __name__ == "__main__":
    unittest.main()
