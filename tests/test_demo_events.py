import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from demo_events import ACTION_STAGES, DemoEvents, MAX_BODY, MAX_EXCHANGES, apply_event


class DemoEvidenceTests(unittest.TestCase):
    def test_complete_setup_finishes_routing_with_weights(self):
        stages = ACTION_STAGES["verify"]
        self.assertLess(stages.index("routing-priority"), stages.index("routing-weighted"))

    def recorder(self):
        events = []
        recorder = DemoEvents(True, events.append)
        recorder.observe_host("gateway.example.invalid", "AI Gateway")
        return recorder, events

    def test_credentials_are_redacted_before_emission_even_when_echoed(self):
        recorder, events = self.recorder()
        secret = "example-sensitive-value-that-is-not-a-real-key"
        handle = recorder.begin("POST", "https://gateway.example.invalid/models", {
            "Authorization": "Bearer " + secret,
            "api-key": secret,
        }, {"model": "example-model"})
        recorder.end(handle, 200, {"Mcp-Session-Id": "example-private-session"}, {
            "echo": secret, "session": "example-private-session", "access_token": secret,
            "usage": {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17},
        })
        serialized = json.dumps(events)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("example-private-session", serialized)
        self.assertEqual(events[-1]["totalTokens"], 17)

    def test_configuration_masks_custom_credential_headers(self):
        recorder, events = self.recorder()
        recorder.configuration("Tool", {"credentials": {"type": "header", "headers": {
            "x-custom-provider-secret": "not-a-real-provider-key",
        }}})
        self.assertNotIn("not-a-real-provider-key", json.dumps(events))
        self.assertIn("x-custom-provider-secret", json.dumps(events))

    def test_management_requests_are_not_recorded(self):
        recorder, events = self.recorder()
        handle = recorder.begin("POST", "https://management.azure.com/listSecrets", {
            "Authorization": "Bearer example-management-token",
        }, None)
        recorder.end(handle, 200, {}, {"primaryKey": "not-a-real-key"})
        self.assertIsNone(handle)
        self.assertEqual(events, [])

    def test_correlation_id_survives_resource_identifier_redaction(self):
        recorder, events = self.recorder()
        resource_id = "00000000-0000-0000-0000-000000000001"
        correlation = "00000000-0000-0000-0000-000000000002"
        recorder.identifier(resource_id, "<subscription-id>")
        recorder.begin("GET", "https://gateway.example.invalid/" + resource_id, {
            "x-correlation-id": correlation,
        }, None)
        self.assertNotIn(resource_id, json.dumps(events))
        self.assertEqual(events[0]["exchange"]["correlationId"], correlation)

    def test_expected_401_is_not_an_unexpected_failure(self):
        recorder, events = self.recorder()
        recorder.expect("Missing credential", (401, 403))
        handle = recorder.begin("POST", "https://gateway.example.invalid/models", {}, {})
        recorder.end(handle, 401, {}, {"error": "Unauthorized"})
        self.assertTrue(events[-1]["expected"])
        self.assertFalse(events[-1]["unexpected"])

    def test_w3c_trace_identifier_is_preserved_for_native_lookup(self):
        recorder, events = self.recorder()
        trace = "a" * 32
        recorder.begin("POST", "https://gateway.example.invalid/models", {
            "traceparent": "00-" + trace + "-" + "b" * 16 + "-01",
        }, {})
        self.assertEqual(events[0]["exchange"]["traceId"], trace)

    def test_recovery_keeps_the_failed_response_visible(self):
        recorder, events = self.recorder()
        recorder.plan("mcp")
        with recorder.stage("ai-gateway-mcp"):
            first = recorder.begin("POST", "https://gateway.example.invalid/mcp", {}, {"method": "tools/call"})
            recorder.end(first, 200, {}, {"result": {"isError": True}})
            recorder.invalid_exchange("Model tool returned invalid content.")
            recorder.notice("Retrying read-only call.", attempt=2)
            second = recorder.begin("POST", "https://gateway.example.invalid/mcp", {}, {"method": "tools/call"})
            recorder.end(second, 200, {}, {"result": {"isError": False}})
        job = {}
        for event in events:
            apply_event(job, event)
        self.assertEqual(job["stages"][0]["status"], "passed")
        self.assertEqual(job["exchanges"][0]["status"], "failed")
        self.assertEqual(job["exchanges"][0]["httpStatus"], 200)
        self.assertEqual(len(job["notices"]), 1)
        self.assertNotIn("failure", job)

    def test_failure_preserves_passed_stages_and_marks_unrun_stages_skipped(self):
        recorder, events = self.recorder()
        recorder.plan("verify")
        with recorder.stage("model-access"):
            pass
        with self.assertRaisesRegex(ValueError, "model tool"):
            with recorder.stage("ai-gateway-mcp"):
                raise ValueError("Invalid model tool response")
        recorder.finish()
        job = {}
        for event in events:
            apply_event(job, event)
        statuses = {stage["id"]: stage["status"] for stage in job["stages"]}
        self.assertEqual(statuses["model-access"], "passed")
        self.assertEqual(statuses["ai-gateway-mcp"], "failed")
        self.assertEqual(statuses["routing-weighted"], "skipped")
        self.assertEqual(job["failure"]["label"], "AI Gateway MCP tool")

    def test_response_bodies_and_capture_count_are_bounded_and_reported(self):
        recorder, events = self.recorder()
        handle = recorder.begin("POST", "https://gateway.example.invalid/models", {}, {})
        recorder.end(handle, 200, {}, "x" * (MAX_BODY + 50), encoding="text")
        self.assertTrue(events[-1]["responseBody"]["truncated"])
        self.assertLessEqual(len(events[-1]["responseBody"]["text"]), MAX_BODY)
        recorder.count = MAX_EXCHANGES
        self.assertIsNone(recorder.begin("GET", "https://gateway.example.invalid/models", {}, None))
        recorder.finish()
        self.assertEqual(events[-1]["type"], "capture-limit")
        self.assertEqual(events[-1]["omitted"], 1)

    def test_unknown_response_id_is_an_explicit_protocol_error(self):
        with self.assertRaisesRegex(ValueError, "matching request"):
            apply_event({"exchanges": []}, {"type": "exchange-end", "id": "unknown"})


if __name__ == "__main__":
    unittest.main()
