"""
core/cloud_tunnel.py — Zero-Config Cloudflare Quick Tunnel Manager for JARVIS
Automatically downloads `cloudflared.exe` if missing, exposes local port 8000
over a globally accessible HTTPS/WSS URL (`https://*.trycloudflare.com`),
and handles self-signed local TLS certificates seamlessly (`--no-tls-verify`).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

_SYSTEM = platform.system()
_BASE = Path(__file__).resolve().parent.parent
_BIN_DIR = _BASE / "bin"
_CLOUDFLARED_EXE = _BIN_DIR / ("cloudflared.exe" if _SYSTEM == "Windows" else "cloudflared")

_DOWNLOAD_URLS = {
    "Windows": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "Linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "Darwin": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
}

_URL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+-[a-zA-Z0-9-]+\.trycloudflare\.com")


class CloudTunnel:
    def __init__(self, local_port: int = 8000, use_https: bool = True):
        self.local_port = local_port
        self.use_https = use_https
        self.public_url: str = ""
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def ensure_binary(self) -> Path | None:
        """Finds cloudflared on PATH or in `bin/`, downloading it automatically if absent."""
        sys_bin = shutil.which("cloudflared")
        if sys_bin:
            return Path(sys_bin)
        if _CLOUDFLARED_EXE.exists() and _CLOUDFLARED_EXE.stat().st_size > 5_000_000:
            return _CLOUDFLARED_EXE

        url = _DOWNLOAD_URLS.get(_SYSTEM)
        if not url or _SYSTEM == "Darwin":
            print("[CloudTunnel] Please install cloudflared manually for this OS.")
            return None

        try:
            _BIN_DIR.mkdir(parents=True, exist_ok=True)
            print(f"[CloudTunnel] ⬇️ Downloading cloudflared binary to {_CLOUDFLARED_EXE}...")
            urllib.request.urlretrieve(url, str(_CLOUDFLARED_EXE))
            if _SYSTEM != "Windows":
                os.chmod(_CLOUDFLARED_EXE, 0o755)
            print("[CloudTunnel] ✅ cloudflared binary ready.")
            return _CLOUDFLARED_EXE
        except Exception as e:
            print(f"[CloudTunnel] ❌ Failed to download cloudflared: {e}")
            return None

    def start(self, timeout: float = 20.0) -> str:
        """Starts the tunnel subprocess and waits up to `timeout` seconds for the public URL."""
        with self._lock:
            if self._proc and self._proc.poll() is None and self.public_url:
                return self.public_url

            binary = self.ensure_binary()
            if not binary:
                return ""

            scheme = "https" if self.use_https else "http"
            target_url = f"{scheme}://127.0.0.1:{self.local_port}"
            cmd = [str(binary), "tunnel", "--url", target_url]
            if self.use_https:
                cmd.append("--no-tls-verify")

            creationflags = subprocess.CREATE_NO_WINDOW if _SYSTEM == "Windows" else 0
            self.public_url = ""
            self._stop_event.clear()

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except Exception as e:
                print(f"[CloudTunnel] ❌ Subprocess start error: {e}")
                return ""

            def _reader():
                assert self._proc and self._proc.stdout
                for line in self._proc.stdout:
                    if self._stop_event.is_set():
                        break
                    m = _URL_REGEX.search(line)
                    if m and not self.public_url:
                        self.public_url = m.group(0)
                        print(f"[CloudTunnel] 🌍 Global Public URL active: {self.public_url}")

            threading.Thread(target=_reader, daemon=True, name="cloudflared-reader").start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.public_url:
                return self.public_url
            if self._proc and self._proc.poll() is not None:
                break
            time.sleep(0.2)

        return self.public_url

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            self.public_url = ""

    @property
    def is_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self.public_url)
