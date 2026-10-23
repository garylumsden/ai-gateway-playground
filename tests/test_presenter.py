import json
import os
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gateway_demo import DemoError
from presenter import ACTIONS, Presenter, outcome, portal, protect_local, require_local_origin, result_summary


class PresenterRulesTests(unittest.TestCase):
    def test_no_deployment_configuration_or_arbitrary_command_actions(self):
        for forbidden in ("deploy", "configure", "configure-mcp", "configure-telemetry", "configure-aliases", "configure-routing", "cleanup-legacy-telemetry", "preflight", "preview", "shell", "smoke;whoami"):
            self.assertNotIn(forbidden, ACTIONS)

    def test_only_local_https_origin_is_accepted(self):
        self.assertEqual(require_local_origin("https://localhost:8443"), "https://localhost:8443")
        for origin in (
            "http://localhost:8443", "https://other.example.invalid:8443",
            "https://localhost.attacker.invalid:8443", "https://localhost:8443/path",
            "https://name:secret@localhost:8443", "https://localhost:8443?query=yes",
        ):
            with self.subTest(origin=origin), self.assertRaises(DemoError):
                require_local_origin(origin)

    def test_known_gap_never_becomes_passed(self):
        self.assertEqual(outcome("telemetry", 1, "", "Native gateway token records contain zero input and output usage."), "known-gap")
        self.assertEqual(outcome("verify", 2, "CORE CHECKS PASSED; NATIVE TOKEN CHECK FAILED.", ""), "known-gap")
        self.assertEqual(outcome("telemetry", 1, "", "Unauthorized"), "failed")
        self.assertEqual(outcome("verify", 2, "usage: invalid command", ""), "failed")
        self.assertEqual(outcome("mcp", 1, "", "Native gateway token records contain zero"), "failed")

    def test_successful_transport_or_configuration_does_not_mask_action_exit(self):
        self.assertEqual(outcome("smoke", 1, "HTTP 200", ""), "failed")
        self.assertEqual(outcome("smoke", 0, "", ""), "passed")

    def test_failed_action_does_not_display_stale_successful_state(self):
        self.assertIsNone(result_summary("smoke", "failed", {"modelChecks": [{"httpStatus": 200}]}))

    def test_worker_failure_finishes_active_and_pending_stages(self):
        from opentelemetry.sdk.trace import TracerProvider

        for error in (DemoError("Invalid evidence."), subprocess.TimeoutExpired("demo-child", 5)):
            with self.subTest(error=type(error).__name__):
                worker = object.__new__(Presenter)
                worker.lock = threading.Lock()
                worker.busy = True
                worker.operator = {"name": "example-operator", "tenantId": "example-directory"}
                worker.demo = SimpleNamespace(
                    config={
                        "subscriptionId": "example-subscription",
                    },
                    state={
                        "hostname": "gateway.example.invalid", "gatewayName": "example-gateway",
                        "applicationInsightsId": "/components/example-insights",
                    },
                )
                worker.provider = TracerProvider()
                worker.tracer = worker.provider.get_tracer("presenter-worker-test")
                job = {
                    "action": "verify", "status": "running",
                    "stages": [
                        {"id": "model-access", "status": "passed"},
                        {"id": "ai-gateway-mcp", "label": "AI Gateway MCP tool", "status": "running"},
                        {"id": "native-tokens", "status": "pending"},
                    ],
                }
                try:
                    with patch("presenter.verify_operator", return_value=worker.operator), patch.object(worker, "execute", side_effect=error):
                        worker.run_job(job)
                    self.assertEqual(job["status"], "failed")
                    self.assertEqual(job["exitCode"], 1)
                    self.assertEqual([stage["status"] for stage in job["stages"]], ["passed", "failed", "skipped"])
                    self.assertEqual(job["failure"]["id"], "ai-gateway-mcp")
                    self.assertFalse(worker.busy)
                    self.assertIn("completedAt", job)
                finally:
                    worker.provider.shutdown()

    def test_resource_links_have_one_resource_separator(self):
        self.assertEqual(
            portal("/subscriptions/example/resourceGroups/demo"),
            "https://portal.azure.com/#resource/subscriptions/example/resourceGroups/demo/overview",
        )

    @unittest.skipUnless(os.name == "nt", "Windows user-scoped protection")
    def test_launch_secret_is_protected_by_current_windows_user(self):
        clear = b"presenter-test-launch-secret"
        protected = protect_local(clear)
        self.assertNotIn(clear, protected)
        self.assertEqual(protect_local(protected, decrypt=True), clear)
        with self.assertRaises(DemoError):
            protect_local(b"invalid-protected-data", decrypt=True)


class PresenterResetTests(unittest.TestCase):
    def setUp(self):
        from opentelemetry.sdk.trace import TracerProvider

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worker = object.__new__(Presenter)
        worker.lock = threading.Lock()
        worker.busy = True
        worker.operator = {"name": "example-operator", "tenantId": "example-directory"}
        worker.links = []
        state = {
            "hostname": "gateway.example.invalid", "gatewayName": "example-gateway",
            "applicationInsightsId": "/components/example-insights", "sku": "Consumption",
            "routingMode": "weighted", "presenterResetAt": "2030-01-15T19:00:00Z",
        }
        path = Path(directory.name) / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        worker.demo = SimpleNamespace(
            config={
                "subscriptionId": "example-subscription",
                "resourceGroupName": "example-group", "model": "example-model",
            },
            state=state, state_path=path,
        )
        worker.provider = TracerProvider()
        self.addCleanup(worker.provider.shutdown)
        worker.tracer = worker.provider.get_tracer("presenter-reset-test")
        self.previous = {"id": "previous", "action": "smoke", "status": "passed", "exchanges": [{"id": "old-request"}]}
        self.reset = {"id": "reset", "action": "reset-demo", "status": "running", "stages": []}
        worker.jobs = {"previous": self.previous, "reset": self.reset}
        self.worker = worker

    def run_reset(self, exit_code):
        result = subprocess.CompletedProcess(["demo-child"], exit_code, "", "" if exit_code == 0 else "Reset failed.")
        with patch("presenter.verify_operator", return_value=self.worker.operator), patch.object(self.worker, "execute", return_value=result):
            self.worker.run_job(self.reset)

    def test_success_clears_previous_jobs_but_keeps_reset_pollable(self):
        self.run_reset(0)
        self.assertEqual(self.reset["status"], "passed")
        self.assertEqual(list(self.worker.jobs), ["reset"])
        self.assertEqual(self.worker.context()["jobs"], [])
        self.assertEqual(self.worker.context()["resetAt"], "2030-01-15T19:00:00Z")
        self.assertEqual(self.reset["summary"]["mode"], "weighted")
        self.assertFalse(self.worker.busy)

    def test_failure_retains_previous_results(self):
        self.run_reset(1)
        self.assertEqual(self.reset["status"], "failed")
        self.assertIs(self.worker.jobs["previous"], self.previous)
        self.assertIn(self.previous, self.worker.context()["jobs"])
        self.assertIn("Reset failed", self.reset["error"])

    def test_reset_remains_visible_while_running(self):
        self.assertIn(self.reset, self.worker.context()["jobs"])

    def test_busy_worker_rejects_reset_without_clearing_jobs(self):
        with self.assertRaises(BlockingIOError):
            self.worker.start_job("reset-demo")
        self.assertIn("previous", self.worker.jobs)

    def test_monitoring_summary_uses_native_check_results(self):
        state = {"activityChecks": {"Requests": 12}, "telemetryChecks": {"InputTokens": 30, "OutputTokens": 9}}
        summary = result_summary("monitoring", "passed", state)
        self.assertEqual(summary["activity"]["Requests"], 12)
        self.assertEqual(summary["tokens"]["OutputTokens"], 9)
        self.assertIsNone(result_summary("monitoring", "failed", state))


if __name__ == "__main__":
    unittest.main()
