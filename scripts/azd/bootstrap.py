"""Run the azd hooks with the project's pinned Python dependencies."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[2]


def python_runtime() -> str:
    if sysconfig.get_platform() != "win-arm64":
        return sys.executable

    # The pinned cryptography release has Windows x64 wheels, but no Windows ARM64 wheel.
    launcher = shutil.which("py")
    if launcher:
        runtimes = subprocess.check_output([launcher, "--list-paths"], text=True)
        for line in runtimes.splitlines():
            match = re.search(r"([A-Za-z]:\\.*python.exe)\s*$", line)
            if not match:
                continue
            executable = match[1]
            platform = subprocess.check_output(
                [
                    executable, "-c",
                    "import sys, sysconfig; print(sysconfig.get_platform() if sys.version_info >= (3, 11) else '')",
                ],
                text=True,
            ).strip()
            if platform == "win-amd64":
                return executable
    raise RuntimeError("Install Python 3.11 or later for Windows x64, including on Windows ARM64.")


def main() -> int:
    if sys.argv[1] == "clean":
        local = ROOT / ".azure" / os.environ["AZURE_ENV_NAME"]
        for name in ("gateway-config.json", "state.json"):
            (local / name).unlink(missing_ok=True)
        print("Removed local gateway settings for this azd environment.")
        return 0

    directory = ROOT / ".venv"
    python = directory / ("Scripts\\python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([python_runtime(), "-m", "venv", str(directory)], check=True)

    lock = ROOT / "requirements-presenter.lock.txt"
    fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
    installed = directory / "requirements.sha256"
    if not installed.exists() or installed.read_text() != fingerprint:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(lock)], check=True)
        installed.write_text(fingerprint, encoding="ascii")

    return subprocess.call([str(python), str(Path(__file__).with_name("lifecycle.py")), *sys.argv[1:]], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
