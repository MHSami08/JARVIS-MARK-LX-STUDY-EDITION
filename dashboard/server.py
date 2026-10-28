"""
dashboard/server.py — JARVIS Local & Global Cloud Dashboard Server

Supports:
- Local LAN HTTP/HTTPS + Automatic Global Cloudflare Tunnel (`https://*.trycloudflare.com`)
- AES-256-CBC encrypted commands & One-Time QR / PIN Pairing + Persistent Device Tokens
- Bidirectional Voice Streaming:
  * `/ws/phone-audio`  (Phone Mic 16kHz PCM -> JARVIS Gemini Live)
  * `/ws/jarvis-audio` (JARVIS 24kHz PCM Voice -> Phone Speaker anywhere in the world)
- Full File Sharing (`/api/upload`, `/api/files`, `/uploads/{filename}`) with auto-binding to JARVIS
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR      = Path(__file__).resolve().parent.parent
STATIC_DIR    = Path(__file__).parent / "static"
PORT          = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()


def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None


_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    import sys, subprocess, os, tempfile, threading

    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule = "JARVIS Dashboard Python"
        py_exe    = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        need_port = not _netsh_rule_exists(port_rule)
        need_prog = not _netsh_rule_exists(prog_rule)
        if not need_port and not need_prog:
            return

        bat_lines = ["@echo off"]
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        try:
            r = subprocess.run([bat_path], capture_output=True, timeout=8, shell=True)
            if r.returncode == 0:
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        try:
            ctypes.windll.shell32.ShellExecuteW(None, "runas", bat_path, None, None, 0)
        except Exception:
            pass
        finally:
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
    except Exception:
        pass


_ensure_crypto_js()

_VIRTUAL_NIC = re.compile(
    r"vethernet|virtualbox|vmware|vmnet|hyper-v|wsl|docker|veth|virbr|br-|tailscale|"
    r"zerotier|radmin|hamachi|wireguard|openvpn|nord|proton|express|\bvpn\b|\btap\b|"
    r"tap-|\btun\b|loopback|bluetooth|pseudo|isatap|teredo|npcap|"
    r"local area connection\*",
    re.I,
)


def _lan_candidates() -> list[tuple[int, str, str]]:
    try:
        import psutil
    except ImportError:
        return []
    out: list[tuple[int, str, str]] = []
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                ip = a.address
                if not ip or ip.startswith(("127.", "169.254.")) or ip == "0.0.0.0":
                    continue
                score = 0
                if ip.startswith("192.168."):
                    score += 50
                elif ip.startswith("10."):
                    score += 30
                elif re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
                    score += 20
                if re.match(r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", ip):
                    score -= 40
                if _VIRTUAL_NIC.search(name):
                    score -= 60
                if re.search(r"wi-?fi|wlan|wireless|wlp|\ben\d|enp|eno|eth\d|ethernet", name, re.I):
                    score += 25
                out.append((score, ip, name))
    except Exception:
        return []
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _configured_ip() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            v = str(_json.load(f).get("dashboard_ip") or "").strip()
        return v or None
    except Exception:
        return None


def _https_wanted() -> bool:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return bool(_json.load(f).get("dashboard_https", True))
    except Exception:
        return True


def _tunnel_wanted() -> bool:
    """Defaults to True so the mobile app works across any network."""
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return bool(_json.load(f).get("enable_cloud_tunnel", True))
    except Exception:
        return True


def _local_ip() -> str:
    manual = _configured_ip()
    if manual:
        return manual
    ranked = _lan_candidates()
    if ranked and ranked[0][0] > -30:
        return ranked[0][1]
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass
    return "127.0.0.1"


def _ensure_certs() -> bool:
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True
    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])
        alt = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return True
    except Exception:
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _client_ip(req: Request) -> str:
    """Extracts true client IP even when routed through Cloudflare Tunnel."""
    cf_ip = req.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else ""


class DashboardServer:

    def __init__(self):
        self._ip                             = _local_ip()
        self.public_url: str                 = ""   # Populated by CloudTunnel when online
        self._tunnel                         = None
        self._tokens: set[str]               = set()
        self._token_keys: dict[str, str]     = {}
        self._aes_cache: dict[str, bytes]    = {}
        self._clients: set[WebSocket]        = set()
        self._audio_out_clients: set[WebSocket] = set()  # Mobile clients listening to JARVIS voice
        self._history: list[dict]            = []
        self._command_queue                  = asyncio.Queue()
        self._wake_callback                  = None
        self._connect_callback               = None
        self._file_upload_callback           = None
        self._pending_keys: dict[str, float] = {}
        self._used_keys: dict[str, tuple]    = {}
        self.ready                           = False
        self.port_conflict                   = False
        self._device_sessions: dict[str, dict] = {}
        self._phone_audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.phone_mic_open                  = 0
        # Set by POST /api/audio-mode. While True the desktop mutes its own
        # speakers and JARVIS's voice goes to the phone only. Cleared when the
        # last phone voice listener drops, so the PC never stays silent.
        self.phone_audio_only                = False
        self._uploads_dir                    = UPLOADS_DIR
        self._login_html                     = _read("login.html")
        self._app_html                       = _read("app.html")
        self.app                             = self._build_app()

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        return False
        if not _https_wanted():
            return False
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_local_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_url(self) -> str:
        """Returns the Global Cloudflare HTTPS URL if active, otherwise the LAN URL."""
        if self.public_url:
            return self.public_url
        return self.get_local_url()

    def get_manual_url(self) -> str:
        if self.public_url:
            return self.public_url.replace("https://", "")
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_file_upload_callback(self, fn) -> None:
        self._file_upload_callback = fn

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast_audio(self, pcm_bytes: bytes) -> None:
        """Streams JARVIS's 24kHz 16-bit mono PCM output audio to connected mobile apps."""
        if not self._audio_out_clients or not pcm_bytes:
            return
        dead: set[WebSocket] = set()
        for ws in list(self._audio_out_clients):
            try:
                # Bounded: the caller fires one task per audio slice, so a phone
                # that stops draining its socket (backgrounded, Wi-Fi power
                # save, tunnel stall) would otherwise leave an unbounded pile of
                # blocked sends holding audio that is already stale. A client
                # that cannot take a slice within two seconds is dropped; the
                # app reconnects on its own, and dropping it here is also what
                # releases `phone_audio_only` so the PC starts speaking again.
                await asyncio.wait_for(ws.send_bytes(pcm_bytes), timeout=2.0)
            except Exception:
                dead.add(ws)
        self._audio_out_clients -= dead

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        @app.get("/api/status")
        async def api_status(req: Request):
            """Public & authenticated health check for the Native Mobile App."""
            authed = _auth(req)
            return JSONResponse({
                "ok": True,
                "online": self.ready,
                "authenticated": authed,
                "public_url": self.public_url,
                "local_url": self.get_local_url(),
                "audio_sample_rate_in": 16000,
                "audio_sample_rate_out": 24000,
                "phone_audio_only": self.phone_audio_only,
                "max_upload_mb": MAX_UPLOAD_MB,
            })

        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE), media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index(req: Request):
            host_hdr = req.headers.get("host") or f"{self._ip}:{PORT}"
            hostname = host_hdr.split(":")[0]
            port_str = host_hdr.split(":")[1] if ":" in host_hdr else ("443" if "trycloudflare.com" in host_hdr else str(PORT))
            html = (self._app_html
                    .replace("__IP__", hostname)
                    .replace("__PORT__", port_str))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]
                tok     = secrets.token_urlsafe(32)
                dev_tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)
                self._device_sessions[dev_tok] = {"session_key": entered}
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                return JSONResponse({
                    "ok": True,
                    "token": tok,
                    "key": entered,
                    "device_token": dev_tok,
                    "public_url": self.public_url,
                })
            return JSONResponse({"ok": False, "error": "Invalid or expired key"}, status_code=401)

        def _problem_page(title: str, text: str) -> HTMLResponse:
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  h2{{color:#f87171;margin-bottom:12px}}p{{color:#8b97ab;font-size:15px;max-width:340px;line-height:1.5}}
</style></head>
<body><div><h2>{title}</h2><p>{text}</p></div></body></html>""")

        def _connected_page(tok: str, key: str, dev_tok: str) -> HTMLResponse:
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS</p>
</body></html>""")

        _REPLAY_SECS = 180

        @app.get("/auto-login")
        async def auto_login(req: Request, key: str = "", format: str = ""):
            now = time.time()
            ip  = _client_ip(req)

            used = self._used_keys.get(key)
            if used and (now - used[0]) < _REPLAY_SECS and used[1] == ip:
                if format == "json" or "application/json" in req.headers.get("accept", ""):
                    return JSONResponse({
                        "ok": True,
                        "token": used[2],
                        "key": key,
                        "device_token": used[3],
                        "public_url": self.public_url,
                    })
                return _connected_page(used[2], key, used[3])
            if used:
                if format == "json":
                    return JSONResponse({"ok": False, "error": "Link already used"}, status_code=401)
                return _problem_page(
                    "Link already used",
                    "This QR code has already been used. Press <b>Remote Control</b> in JARVIS to get a new one.")

            if key and key in self._pending_keys and self._pending_keys[key] <= now:
                if format == "json":
                    return JSONResponse({"ok": False, "error": "Link expired"}, status_code=401)
                return _problem_page(
                    "Link expired",
                    "This QR code is older than 10 minutes. Press <b>Remote Control</b> in JARVIS to get a new one.")

            if not key or key not in self._pending_keys:
                if format == "json":
                    return JSONResponse({"ok": False, "error": "Invalid key"}, status_code=401)
                return _problem_page(
                    "Wrong JARVIS",
                    "This code was not made by the JARVIS answering here.")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}
            self._used_keys = {k: v for k, v in self._used_keys.items() if now - v[0] < 900}
            self._used_keys[key] = (now, ip, tok, dev_tok)

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))
            if format == "json" or "application/json" in req.headers.get("accept", ""):
                return JSONResponse({
                    "ok": True,
                    "token": tok,
                    "key": key,
                    "device_token": dev_tok,
                    "public_url": self.public_url,
                })
            return _connected_page(tok, key, dev_tok)

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({
                "ok": True,
                "token": tok,
                "key": session_key,
                "public_url": self.public_url,
            })

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone-only voice mode ─────────────────────────────────────────────
        # The mobile app's speaker button toggles this. While on, the main
        # process stops writing JARVIS's voice to the PC speakers and the audio
        # still streams over /ws/jarvis-audio to the phone.
        @app.post("/api/audio-mode")
        async def audio_mode(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            self.phone_audio_only = bool(body.get("phone_only", True))
            asyncio.create_task(self.broadcast({
                "type": "sys",
                "text": ("Phone audio mode ON — JARVIS speaks on your phone only."
                         if self.phone_audio_only else
                         "Phone audio mode OFF — JARVIS speaks on the PC again."),
            }))
            return JSONResponse({"ok": True, "phone_audio_only": self.phone_audio_only})

        # ── Phone mic real-time audio → Gemini Live (16kHz PCM) ──────────────
        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self.phone_mic_open += 1
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass
            except WebSocketDisconnect:
                pass
            finally:
                self.phone_mic_open = max(0, self.phone_mic_open - 1)
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── JARVIS voice output → Phone speaker (24kHz PCM) ──────────────────
        @app.websocket("/ws/jarvis-audio")
        async def jarvis_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._audio_out_clients.add(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                self._audio_out_clients.discard(websocket)
                # No phone listening any more: never leave the PC muted.
                if not self._audio_out_clients:
                    self.phone_audio_only = False

        # ── File sharing ──────────────────────────────────────────────────────
        def _safe_filename(raw: str) -> str:
            name = Path(raw).name
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                # Notify main JARVIS session so `ui.current_file` is updated immediately!
                if self._file_upload_callback:
                    try:
                        self._file_upload_callback(str(dest))
                    except Exception as e:
                        print(f"[Dashboard] File callback error: {e}")

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "path": str(dest),
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({
                    "ok": True,
                    "name": dest.name,
                    "path": str(dest),
                    "size": size,
                })
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    async def _serve_alias(self) -> None:
        try:
            ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
            ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
            asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
            cfg = uvicorn.Config(
                self.app, host="0.0.0.0", port=PORT + 1, log_level="warning", log_config=None,
                ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
            )
            await uvicorn.Server(cfg).serve()
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass

    async def serve(self) -> None:
        try:
            await self._serve_inner()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            print(f"[Dashboard] Server error: {e}")

    async def _serve_inner(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            return

        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        if _https_wanted():
            _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if not _port_free(PORT):
            self.port_conflict = True
            print(f"[Dashboard] Port {PORT} is already in use by another program.")
            return

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning", log_config=None,
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] Local LAN: {proto}://{self._ip}:{PORT}")
        server = uvicorn.Server(cfg)

        async def _watch_ready() -> None:
            for _ in range(200):
                if server.started:
                    self.ready = True
                    # Start Global Cloudflare Tunnel in background once local server is listening
                    if _tunnel_wanted():
                        try:
                            from core.cloud_tunnel import CloudTunnel
                            self._tunnel = CloudTunnel(local_port=PORT, use_https=use_ssl)
                            pub = await asyncio.to_thread(self._tunnel.start, 25.0)
                            if pub:
                                self.public_url = pub
                                print(f"[Dashboard] 🌍 Global Mobile App URL: {pub}")
                                print("[Dashboard] Press 'Remote Control' in JARVIS UI to scan the Global QR Code.")
                        except Exception as e:
                            print(f"[Dashboard] CloudTunnel note: {e}")
                    return
                await asyncio.sleep(0.1)

        asyncio.create_task(_watch_ready())
        try:
            await server.serve()
        finally:
            self.ready = False
            if self._tunnel:
                self._tunnel.stop()
