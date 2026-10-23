import http.cookiejar
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import ssl
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from presenter import LOCAL, launch_info, launch_url, require_local_origin


@unittest.skipUnless(os.environ.get("PRESENTER_E2E") == "1", "Opt in to the real running HTTPS presenter")
class LivePresenterHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = launch_info()
        cls.origin = require_local_origin(cls.info["origin"])
        cls.context = ssl.create_default_context(cafile=str(LOCAL / "localhost.crt"))
        cls.cookies = http.cookiejar.CookieJar()
        cls.client = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=cls.context),
            urllib.request.HTTPCookieProcessor(cls.cookies),
        )
        token = launch_url(cls.info).split("#launch=", 1)[1]
        cls.launch_token = token
        cls.csrf = ""
        status, headers, result = cls.call("/api/session", "POST", {"token": token})
        if status != 200:
            raise AssertionError("Local operator bootstrap failed.")
        cls.csrf = result["csrf"]
        cls.cookie_header = headers.get("Set-Cookie", "")

    @classmethod
    def call(cls, path, method="GET", body=None, headers=None, authenticated=True):
        merged = {"Content-Type": "application/json"}
        if method == "POST":
            merged["Origin"] = cls.origin
            if authenticated:
                merged["X-CSRF-Token"] = cls.csrf
        merged.update(headers or {})
        request = urllib.request.Request(
            cls.origin + path, method=method, headers=merged,
            data=json.dumps(body).encode() if body is not None else None,
        )
        client = cls.client if authenticated else urllib.request.build_opener(urllib.request.HTTPSHandler(context=cls.context))
        try:
            with client.open(request, timeout=20) as response:
                content = response.read()
                parsed = json.loads(content) if response.headers.get_content_type() == "application/json" else content.decode()
                return response.status, response.headers, parsed
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, json.loads(error.read())

    def test_unauthenticated_browser_cannot_read_deployment_context(self):
        self.assertEqual(self.call("/api/context", authenticated=False)[0], 401)

    def test_private_session_cookie_has_required_flags(self):
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, self.cookie_header)

    def test_opening_another_authorized_tab_preserves_the_first_tab_csrf(self):
        status, _, result = self.call("/api/session", "POST", {"token": self.launch_token})
        self.assertEqual(status, 200)
        self.assertEqual(result["csrf"], self.csrf)
        self.assertEqual(self.call("/api/jobs", "POST", {"action": "deploy"})[0], 400)

    def test_cross_origin_action_is_rejected(self):
        for action in ("smoke", "reset-demo"):
            self.assertEqual(self.call("/api/jobs", "POST", {"action": action}, {"Origin": "https://other.example.invalid"})[0], 403)

    def test_missing_csrf_is_rejected(self):
        for action in ("smoke", "reset-demo"):
            self.assertEqual(self.call("/api/jobs", "POST", {"action": action}, {"X-CSRF-Token": ""})[0], 403)

    def test_unicode_authorization_values_are_rejected_without_handler_errors(self):
        self.assertEqual(self.call("/api/session", "POST", {"token": "\u00e9"}, authenticated=False)[0], 401)
        self.assertEqual(self.call("/api/jobs", "POST", {"action": "smoke"}, {"X-CSRF-Token": "\u00e9"})[0], 403)

    def test_deployment_and_arbitrary_actions_are_not_exposed(self):
        for action in ("deploy", "configure", "configure-mcp", "shell", "smoke;whoami"):
            with self.subTest(action=action):
                self.assertEqual(self.call("/api/jobs", "POST", {"action": action})[0], 400)

    def test_extra_action_parameters_are_rejected(self):
        self.assertEqual(self.call("/api/jobs", "POST", {"action": "smoke", "command": "unexpected"})[0], 400)

    def test_host_header_is_checked(self):
        self.assertEqual(self.call("/api/context", headers={"Host": "other.example.invalid"})[0], 421)

    def test_security_headers_protect_api_responses(self):
        status, headers, _ = self.call("/api/context")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_presenter_guidance_stays_in_the_help_dialog(self):
        class PageStructure(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_help = False
                self.help_ids = set()
                self.public_ids = set()

            def handle_starttag(self, tag, attributes):
                attributes = dict(attributes)
                if tag == "dialog" and attributes.get("id") == "presenter-help":
                    self.in_help = True
                if "id" in attributes:
                    (self.help_ids if self.in_help else self.public_ids).add(attributes["id"])

            def handle_endtag(self, tag):
                if tag == "dialog":
                    self.in_help = False

        status, _, content = self.call("/")
        self.assertEqual(status, 200)
        page = PageStructure()
        page.feed(content)
        for identifier in ("speaker-note", "caution", "execution-detail", "operator-name", "operator-subscription", "operator-tenant", "help-resources"):
            self.assertIn(identifier, page.help_ids)
            self.assertNotIn(identifier, page.public_ids)
        self.assertIn("open-help", page.public_ids)
        self.assertIn("step-links", page.public_ids)
        self.assertNotIn("PRESENTER WORKSPACE", content)

    def test_context_contains_real_links_but_no_cloud_credentials(self):
        status, _, result = self.call("/api/context")
        self.assertEqual(status, 200)
        self.assertEqual(result["operator"]["type"], "Azure CLI user")
        self.assertEqual({link["id"] for link in result["resources"]}, {
            "newGateway", "gatewayPortal", "newInsights", "newGroup", "workspace", "foundry", "gatewayAudit",
        })
        for link in result["resources"]:
            self.assertTrue(link["href"] is None or link["href"].startswith("https://"))
            self.assertNotEqual(link["id"], "nativeMetrics")
            self.assertNotIn("microsoft.monitor/accounts", (link["href"] or "").lower())
        serialized = json.dumps(result)
        for forbidden in ('"accessToken"', '"primaryKey"', '"secondaryKey"', '"connectionString"'):
            self.assertNotIn(forbidden, serialized)

    def test_private_runbook_contains_exact_links_not_placeholders(self):
        status, _, result = self.call("/api/runbook")
        self.assertEqual(status, 200)
        self.assertIn("https://portal.azure.com/#resource/subscriptions/", result)
        self.assertNotIn("<subscription-id>", result)
        self.assertIn("Do not commit", result)
        self.assertNotIn("Native metrics workspace", result)
        self.assertNotIn("microsoft.monitor/accounts", result.lower())

    def test_public_copy_does_not_qualify_demo_inputs(self):
        for path in ("/", "/app.js", "/demo_content.js"):
            with self.subTest(path=path):
                status, _, content = self.call(path)
                self.assertEqual(status, 200)
                self.assertNotRegex(content.lower(), r"\b(fictional|synthetic)\b")


if __name__ == "__main__":
    unittest.main()
