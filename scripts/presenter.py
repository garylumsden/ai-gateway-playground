"""Local HTTPS presenter using the operator's approved Azure CLI user identity."""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid
import webbrowser

from gateway_demo import Demo, DemoError, ROOT, ROUTING_WEIGHTS, az
from redaction import redact
from demo_events import ACTION_STAGES, STAGE_LABELS, DemoEvents, PREFIX, apply_event

WEB = ROOT / "web"
LOCAL = ROOT / ".azure" / "presenter"
COOKIE = "__Host-gateway-presenter"
ACTIONS = frozenset(("smoke", "rate", "mcp", "activity", "telemetry", "monitoring", "verify", "routing-weighted", "routing-priority", "reset-demo"))
LIFETIME = 8 * 60 * 60
SESSION_LIFETIME = 2 * 60 * 60
TELEMETRY_ROLE = "ai-gateway-playground-presenter"
ENTROPY = b"ai-gateway-playground-presenter-local-session-v1"


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_local(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise DemoError("This launcher requires Windows user-scoped data protection.")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    value_buffer = ctypes.create_string_buffer(value)
    entropy_buffer = ctypes.create_string_buffer(ENTROPY)
    incoming = DataBlob(len(value), ctypes.cast(value_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = DataBlob(len(ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = DataBlob()
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(incoming), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing)):
        raise DemoError("Windows user-scoped data protection failed.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel.LocalFree(outgoing.data)


def tls_context() -> ssl.SSLContext:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    LOCAL.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    pfx_path = LOCAL / "localhost.pfx"
    environment = dict(os.environ, PRESENTER_CERT_PASSWORD=password)
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "scripts" / "export-presenter-certificate.ps1"),
         "-Destination", str(pfx_path)],
        env=environment, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        pfx_path.unlink(missing_ok=True)
        raise DemoError("A trusted localhost HTTPS certificate is required. No certificate trust was changed.")
    try:
        key, certificate, _ = pkcs12.load_key_and_certificates(pfx_path.read_bytes(), password.encode())
        if key is None or certificate is None:
            raise DemoError("The localhost certificate does not contain an exportable private key.")
        certificate_bytes = certificate.public_bytes(serialization.Encoding.PEM)
        (LOCAL / "localhost.crt").write_bytes(certificate_bytes)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        with tempfile.TemporaryDirectory(dir=LOCAL) as directory:
            cert_file = Path(directory) / "certificate.pem"
            key_file = Path(directory) / "private-key.pem"
            cert_file.write_bytes(certificate_bytes)
            key_file.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(key_file, 0o600)
            context.load_cert_chain(cert_file, key_file)
        return context
    finally:
        pfx_path.unlink(missing_ok=True)


def verify_operator(demo: Demo) -> dict:
    account = az(["account", "show", "--subscription", demo.subscription])
    if not isinstance(account, dict) or account.get("id", "").lower() != demo.subscription.lower():
        raise DemoError("Azure CLI is not using the configured subscription.")
    user = account.get("user", {})
    if user.get("type", "").lower() != "user" or not user.get("name"):
        raise DemoError("Start the presenter with an Azure CLI user session, not a service principal or managed identity.")
    demo.token()
    return {
        "name": user["name"], "tenantId": account["tenantId"],
        "subscriptionName": account["name"], "type": "Azure CLI user",
    }


def portal(resource_id: str, section: str = "overview") -> str:
    return "https://portal.azure.com/#resource" + resource_id + "/" + section


def deployed_resources(demo: Demo) -> list[dict]:
    new_group = demo.prefix + "/resourceGroups/" + demo.config["resourceGroupName"]
    return [
        {"id": "newGateway", "label": "AI Gateway resource", "name": demo.state["gatewayName"],
         "group": "AI Gateway", "href": portal(demo.gateway_id)},
        {"id": "gatewayPortal", "label": "AI Gateway portal", "name": demo.state["gatewayName"],
         "group": "AI Gateway", "href": "https://ai.gateway.azure.com/"},
        {"id": "newInsights", "label": "AI Gateway Application Insights",
         "name": demo.state["applicationInsightsId"].rsplit("/", 1)[-1],
         "group": "AI Gateway", "href": portal(demo.state["applicationInsightsId"])},
        {"id": "newGroup", "label": "AI Gateway resource group", "name": demo.config["resourceGroupName"],
         "group": "AI Gateway", "href": portal(new_group)},
        {"id": "workspace", "label": "Workspace Logs", "name": demo.state["logWorkspaceName"],
         "group": "AI Gateway", "href": portal(demo.state["logWorkspaceId"], "logs")},
        {"id": "foundry", "label": "Playground Foundry account", "name": demo.state["foundryName"],
         "group": "AI Gateway", "href": portal(demo.foundry_id)},
        {"id": "gatewayAudit", "label": "AI Gateway activity log", "name": demo.state["gatewayName"],
         "group": "AI Gateway", "href": portal(demo.gateway_id, "activitylog")},
    ]


def outcome(action: str, exit_code: int, stdout: str, stderr: str) -> str:
    if exit_code == 0:
        return "passed"
    if action == "telemetry" and exit_code == 1 and "native gateway token records contain zero" in stderr.lower():
        return "known-gap"
    if action == "verify" and exit_code == 2 and "CORE CHECKS PASSED; NATIVE TOKEN CHECK FAILED" in stdout:
        return "known-gap"
    return "failed"


def result_summary(action: str, status: str, state: dict) -> object:
    if status == "failed":
        return None
    names = {
        "smoke": "modelChecks", "rate": "rateChecks", "mcp": "mcpChecks",
        "activity": "activityChecks",
    }
    if action == "telemetry":
        return state.get("nativeTokenGap") if status == "known-gap" else state.get("telemetryChecks")
    if action == "monitoring":
        return {"activity": state["activityChecks"], "tokens": state["telemetryChecks"]}
    if action == "reset-demo":
        return {"mode": state["routingMode"], "resetAt": state["presenterResetAt"]}
    if action == "verify":
        return {
            "coreChecksPassed": state.get("coreChecksPassed", False),
            "nativeTokensPassed": state.get("verified", False),
            "status": state.get("setupStatus"),
        }
    if action in ("routing-weighted", "routing-priority"):
        return state.get("routingChecks", {}).get(action)
    return state.get(names.get(action, ""))


class Presenter:
    def __init__(self, config: Path):
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.demo = Demo(config)
        self.operator = verify_operator(self.demo)
        gateway = self.demo.arm(self.demo.gateway_id, version="2026-09-01-preview")
        if gateway["type"].lower() != "microsoft.apimanagement/aigateways":
            raise DemoError("The configured gateway is not the current AI Gateway resource type.")
        insights = self.demo.arm(self.demo.state["applicationInsightsId"], version="2020-02-02")
        self.links = deployed_resources(self.demo)
        self.provider = TracerProvider(resource=Resource({
            "service.name": TELEMETRY_ROLE, "service.instance.id": "local",
        }))
        self.provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter(
            connection_string=insights["properties"]["ConnectionString"],
            disable_offline_storage=True,
        )))
        self.tracer = self.provider.get_tracer("gateway-presenter")
        self.started = time.monotonic()
        self.launch_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.busy = False
        self.origin = ""
        self.config_path = config

    def live(self) -> bool:
        return time.monotonic() - self.started < LIFETIME

    def context(self) -> dict:
        state = json.loads(self.demo.state_path.read_text(encoding="utf-8"))
        return {
            "operator": self.operator,
            "resources": self.links,
            "aiGatewayGroup": self.demo.config["resourceGroupName"],
            "model": self.demo.config["model"],
            "sku": self.demo.state["sku"],
            "identityMode": "Local server using the approved Azure CLI user. This is not browser Entra sign-in.",
            "telemetry": "Presenter internal spans and local request/response evidence. Native metrics come from the gateway.",
            "busy": self.busy,
            "resetAt": state.get("presenterResetAt"),
            "jobs": [job for job in self.jobs.values() if job["action"] != "reset-demo" or job["status"] == "running"],
            "routing": {
                "models": [target["model"] for target in state.get("routingModels", [])],
                "mode": state.get("routingMode", "weighted"),
                "weights": list(ROUTING_WEIGHTS),
            },
            "executionStages": {
                action: [{"id": stage, "label": STAGE_LABELS[stage], "status": "pending"} for stage in stages]
                for action, stages in ACTION_STAGES.items()
            },
        }

    def start_job(self, action: str) -> dict:
        if action not in ACTIONS:
            raise ValueError("Action not allowed.")
        with self.lock:
            if self.busy:
                raise BlockingIOError("A demo check is already running.")
            self.busy = True
            job = {
                "id": uuid.uuid4().hex, "action": action, "status": "running",
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "output": "", "error": "", "summary": None, "exitCode": None,
                "stages": [], "exchanges": [], "configurations": [], "failure": None, "progress": "Starting",
            }
            self.jobs[job["id"]] = job
            while len(self.jobs) > 24:
                del self.jobs[next(iter(self.jobs))]
            snapshot = dict(job)
        threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
        return snapshot

    def execute(self, job: dict) -> subprocess.CompletedProcess:
        arguments = [
            sys.executable, "-u", str(ROOT / "scripts" / "gateway_demo.py"),
            job["action"], "--config", str(self.config_path),
        ]
        process = subprocess.Popen(
            arguments, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            env=dict(os.environ, AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no", PYTHONIOENCODING="utf-8", DEMO_EVENTS="1"),
        )
        messages = queue.Queue()
        output = {"stdout": "", "stderr": ""}
        sanitizer = DemoEvents()
        sanitizer.configure(self.demo.config, self.demo.state)
        sanitizer.identifier(self.operator["tenantId"], "<directory-id>")

        def read_pipe(name, pipe):
            try:
                for line in pipe:
                    messages.put((name, line))
            finally:
                pipe.close()
                messages.put((name, None))

        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            threading.Thread(target=read_pipe, args=(name, pipe), daemon=True).start()
        deadline = time.monotonic() + 600
        closed = 0
        try:
            while closed < 2:
                if time.monotonic() >= deadline:
                    raise DemoError("The check exceeded ten minutes. It did not complete.")
                try:
                    name, line = messages.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    closed += 1
                    continue
                if name == "stdout" and line.startswith(PREFIX):
                    event = json.loads(line[len(PREFIX):])
                    if not isinstance(event, dict):
                        raise DemoError("The execution evidence frame is invalid.")
                    event = sanitizer.clean(event)
                    with self.lock:
                        apply_event(job, event)
                else:
                    safe = redact(line, self.demo.config, self.demo.state)
                    limit = 24000 if name == "stdout" else 8000
                    output[name] = (output[name] + safe)[-limit:]
                    with self.lock:
                        job["output" if name == "stdout" else "error"] = output[name]
            return subprocess.CompletedProcess(arguments, process.wait(timeout=5), output["stdout"], output["stderr"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def run_job(self, job: dict) -> None:
        from opentelemetry.trace import SpanKind, Status, StatusCode

        action = job["action"]
        started = time.monotonic()
        try:
            with self.tracer.start_as_current_span(
                "presenter." + action, kind=SpanKind.INTERNAL,
                record_exception=False, set_status_on_exception=False,
            ) as span:
                span.set_attribute("demo.action", action)
                span.set_attribute("demo.outcome", "failed")
                span.set_status(Status(StatusCode.ERROR))
                current = verify_operator(self.demo)
                if current["name"] != self.operator["name"] or current["tenantId"] != self.operator["tenantId"]:
                    raise DemoError("The Azure CLI user changed. Restart the presenter under the approved user.")
                result = self.execute(job)
                state = json.loads(self.demo.state_path.read_text(encoding="utf-8"))
                status = outcome(action, result.returncode, result.stdout, result.stderr)
                output = redact(result.stdout, self.demo.config, state)[-24000:]
                error = redact(result.stderr, self.demo.config, state)[-8000:]
                with self.lock:
                    job.update({
                        "status": status, "exitCode": result.returncode,
                        "output": output, "error": error,
                        "summary": result_summary(action, status, state),
                    })
                    if action == "reset-demo" and status == "passed":
                        self.jobs.clear()
                        self.jobs[job["id"]] = job
                span.set_attribute("demo.exit_code", result.returncode)
                span.set_attribute("demo.outcome", status)
                span.set_status(Status(StatusCode.OK if status == "passed" else StatusCode.ERROR))
        except (DemoError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            with self.lock:
                message = redact(str(error), self.demo.config, self.demo.state)
                job.update({
                    "status": "failed",
                    "error": message, "exitCode": 1,
                })
                for stage in job.get("stages", []):
                    if stage["status"] == "running":
                        stage.update({"status": "failed", "error": message})
                        job["failure"] = dict(stage)
                    elif stage["status"] == "pending":
                        stage["status"] = "skipped"
        finally:
            with self.lock:
                job["durationSeconds"] = round(time.monotonic() - started, 1)
                job["completedAt"] = datetime.now(timezone.utc).isoformat()
                self.busy = False
            self.provider.force_flush(timeout_millis=5000)

    def private_runbook(self) -> str:
        lines = [
            "# Playground resource links", "",
            "Private operator copy. Do not commit this file or publish its identifiers.", "",
            f"Local app: {self.origin}", "",
            "Use the launch script to authorize a browser session.", "",
        ]
        for link in self.links:
            lines.append(
                f"- [{link['label']}]({link['href']}) - {link['name']}"
                if link["href"] else f"- {link['label']}: unavailable"
            )
        lines += [
            "", "## Clickthrough sequence", "",
            "Overview, model access, routing, request limits, MCP, Monitoring, native spans, and native token counters.",
            "Use Check Setup before presenting. Reset demo clears local results and restores weighted routing.",
            "Run one check at a time. Read stage failures and actual request/response evidence.",
            "Your local Azure CLI user authorizes the server. Gateway keys stay server-side.",
        ]
        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "GatewayPresenter"

    @property
    def app(self) -> Presenter:
        return self.server.presenter

    def log_message(self, format, *args) -> None:
        return

    def send(self, status: int, content: bytes, mime: str = "application/json", cookie: str | None = None) -> None:
        self.send_response(status)
        for name, value in {
            "Content-Type": mime, "Content-Length": str(len(content)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        }.items():
            self.send_header(name, value)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def json(self, status: int, value: object, cookie: str | None = None) -> None:
        self.send(status, json.dumps(value).encode(), cookie=cookie)

    def valid_host(self) -> bool:
        return self.headers.get("Host") == urlparse(self.app.origin).netloc and self.client_address[0] == "127.0.0.1"

    def session(self) -> dict | None:
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        key = cookies[COOKIE].value if COOKIE in cookies else ""
        with self.app.lock:
            session = self.app.sessions.get(key)
            if session and time.monotonic() >= session["expires"]:
                del self.app.sessions[key]
                session = None
        return session if session and self.app.live() else None

    def do_GET(self) -> None:
        if not self.valid_host():
            return self.json(421, {"error": "Host not allowed."})
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        files = {
            "/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"),
            "/demo_content.js": ("demo_content.js", "text/javascript"), "/app.css": ("app.css", "text/css"),
        }
        if path in files:
            name, mime = files[path]
            return self.send(200, (WEB / name).read_bytes(), mime)
        if path == "/api/health":
            return self.json(200, {"application": "ai-gateway-playground-presenter", "live": self.app.live()})
        session = self.session()
        if session is None:
            return self.json(401, {"error": "Open the presenter with the local launch script to authorize this browser."})
        if path == "/api/session":
            return self.json(200, {"csrf": session["csrf"]})
        if path == "/api/context":
            with self.app.lock:
                return self.json(200, self.app.context())
        if path == "/api/runbook":
            return self.send(200, self.app.private_runbook().encode(), "text/markdown; charset=utf-8")
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", path)
        if match:
            with self.app.lock:
                job = self.app.jobs.get(match[1])
                return self.json(200, job) if job else self.json(404, {"error": "Check not found."})
        return self.json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self.valid_host() or self.headers.get("Origin") != self.app.origin:
            return self.json(403, {"error": "Same-origin requests are required."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("Invalid request length.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("A JSON object is required.")
        except (ValueError, json.JSONDecodeError):
            return self.json(400, {"error": "Invalid JSON request."})
        path = urlparse(self.path).path
        if path == "/api/session":
            token = body.get("token")
            if set(body) != {"token"} or not isinstance(token, str) or not self.app.live() or not secrets.compare_digest(token.encode(), self.app.launch_token.encode()):
                return self.json(401, {"error": "The local launch authorization is invalid or expired."})
            session = self.session()
            key = session["id"] if session else secrets.token_urlsafe(32)
            session = session or {"id": key, "csrf": secrets.token_urlsafe(32)}
            session["expires"] = time.monotonic() + SESSION_LIFETIME
            with self.app.lock:
                expired = [key for key, value in self.app.sessions.items() if time.monotonic() >= value["expires"]]
                for expired_key in expired:
                    del self.app.sessions[expired_key]
                while key not in self.app.sessions and len(self.app.sessions) >= 16:
                    del self.app.sessions[next(iter(self.app.sessions))]
                self.app.sessions[key] = session
            return self.json(
                200, {"csrf": session["csrf"]},
                cookie=f"{COOKIE}={key}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_LIFETIME}",
            )
        session = self.session()
        if session is None:
            return self.json(401, {"error": "Authorize this local browser session first."})
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()):
            return self.json(403, {"error": "The local request authorization is invalid."})
        if path != "/api/jobs" or set(body) != {"action"} or not isinstance(body["action"], str):
            return self.json(400, {"error": "Only an approved demo action is accepted."})
        try:
            return self.json(202, self.app.start_job(body["action"]))
        except ValueError:
            return self.json(400, {"error": "Action not allowed."})
        except BlockingIOError:
            return self.json(409, {"error": "A check is already running. Wait for it to finish."})


def launch_info() -> dict:
    return json.loads((LOCAL / "server.json").read_text(encoding="utf-8"))


def require_local_origin(origin: str) -> str:
    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError as error:
        raise DemoError("The presenter origin must be an HTTPS localhost address.") from error
    if (
        parsed.scheme != "https" or parsed.hostname != "localhost"
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        or port is None or not 1 <= port <= 65535
    ):
        raise DemoError("The presenter origin must be an HTTPS localhost address.")
    return origin


def launch_url(info: dict) -> str:
    token = protect_local(base64.b64decode(info["protectedLaunchToken"]), decrypt=True).decode()
    return require_local_origin(info["origin"]) + "/#launch=" + token


def open_existing() -> bool:
    path = LOCAL / "server.json"
    if not path.exists():
        return False
    info = launch_info()
    require_local_origin(info["origin"])
    from gateway_demo import request
    try:
        status, _, health = request("GET", info["origin"] + "/api/health", {})
    except DemoError:
        return False
    if status != 200 or not isinstance(health, dict) or health.get("application") != "ai-gateway-playground-presenter" or not health.get("live"):
        return False
    if not webbrowser.open(launch_url(info)):
        raise DemoError("The default browser did not open. Check its configuration, then run the launcher again.")
    print("Opened the existing private presenter browser session.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / ".azure" / "config.json")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535.")
    try:
        if not args.no_browser and open_existing():
            return 0
        print("Checking the Azure CLI user and exact deployed resource links.", flush=True)
        app = Presenter(args.config.resolve())
        context = tls_context()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.daemon_threads = True
        server.presenter = app
        server.socket = context.wrap_socket(server.socket, server_side=True)
        app.origin = f"https://localhost:{server.server_port}"
        info = {
            "origin": app.origin, "pid": os.getpid(),
            "protectedLaunchToken": base64.b64encode(protect_local(app.launch_token.encode())).decode(),
        }
        (LOCAL / "server.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        (ROOT / ".azure" / "PRESENTER-LIVE.md").write_text(app.private_runbook(), encoding="utf-8")
        print("Presenter ready: " + app.origin, flush=True)
        print("Azure CLI user verified. This app uses local browser authorization.", flush=True)
        print("Azure tokens and gateway keys stay on the server. Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            if not webbrowser.open(launch_url(info)):
                raise DemoError("The default browser did not open. Check its configuration, then run the launcher again.")
        def expire() -> None:
            time.sleep(LIFETIME)
            while app.busy:
                time.sleep(1)
            server.shutdown()
        threading.Thread(target=expire, daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.provider.shutdown()
        return 0
    except (DemoError, OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
