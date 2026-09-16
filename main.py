# ============================================================
# Panel
# Railway Ready
# ============================================================
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, parse_qs
import aiofiles
import httpx
import uvicorn
from fastapi import (
    FastAPI,
    Request,
    HTTPException,
    Depends,
)
from fastapi.responses import (
    Response,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.middleware.cors import CORSMiddleware

# ============================================================
# APP
# ============================================================

APP_NAME = "AGN021G"
APP_VERSION = "14.1.0"

# برند و پشتیبانی AGN021G
SUPPORT_USERNAME = "AGN021G"
SUPPORT_URL = "https://t.me/AGN021G"
SUPPORT_CHANNEL = "https://t.me/AGN021G1388"
SUPPORT_GROUP = "https://t.me/AGN021GCHAT"
PANEL_GITHUB = "https://github.com/agn021g/PANEL-AGN021G"
PANEL_GITHUB_RAW = "https://raw.githubusercontent.com/agn021g/PANEL-AGN021G/main"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(APP_NAME)

# ============================================================
# TIMEZONE
# ============================================================

try:
    from zoneinfo import ZoneInfo

    IRAN_TZ = ZoneInfo("Asia/Tehran")

except Exception:
    IRAN_TZ = None


# ============================================================
# RAILWAY
# ============================================================

PORT = int(
    os.environ.get(
        "PORT",
        "8000",
    )
)

DATA_DIR = Path(
    os.environ.get(
        "RAILWAY_VOLUME_MOUNT_PATH",
        os.environ.get(
            "DATA_DIR",
            "./data",
        ),
    )
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

DATA_FILE = DATA_DIR / "panel_state.json"
TG_FILE = DATA_DIR / "telegram_settings.json"

SECRET_FILE = DATA_DIR / "panel_secret.key"
PANEL_PATH_FILE = DATA_DIR / "panel_path.txt"


def load_or_create_panel_path() -> str:
    """Secret path prefix so /login and /dashboard are not public.
    Priority: ENV PANEL_PATH → saved file (if user-changed) → panel-admin.
    Old auto-generated hex paths are migrated to panel-admin.
    """
    DEFAULT_PANEL_PATH = "panel-admin"
    env = (os.environ.get("PANEL_PATH") or "").strip().strip("/")
    if env and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", env):
        try:
            PANEL_PATH_FILE.write_text(env, encoding="utf-8")
        except Exception:
            pass
        return env
    try:
        if PANEL_PATH_FILE.exists():
            stored = PANEL_PATH_FILE.read_text(encoding="utf-8").strip().strip("/")
            if stored and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", stored):
                # migrate old random 16-hex tokens to default
                if re.fullmatch(r"[0-9a-f]{16}", stored.lower()):
                    try:
                        PANEL_PATH_FILE.write_text(DEFAULT_PANEL_PATH, encoding="utf-8")
                    except Exception:
                        pass
                    return DEFAULT_PANEL_PATH
                return stored
    except Exception:
        pass
    try:
        PANEL_PATH_FILE.write_text(DEFAULT_PANEL_PATH, encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist PANEL_PATH: %s", exc)
    return DEFAULT_PANEL_PATH


PANEL_PATH = load_or_create_panel_path()


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_public_path(path: str) -> bool:
    """Paths reachable without secret PANEL_PATH (subs, health, public pages)."""
    if path in ("/health", "/sub-all"):
        return True
    public_prefixes = (
        "/sub/",
        "/sub-group/",
        "/p/",
        "/info/",
        "/api/public/",
        "/telegram/webhook",
    )
    for pref in public_prefixes:
        if path == pref.rstrip("/") or path.startswith(pref):
            return True
    return False


@app.middleware("http")
async def panel_secret_path_middleware(request: Request, call_next):
    """
    Force admin UI/API behind /{PANEL_PATH}/...
    Public subscription routes stay open.
    Unknown paths without the secret prefix return 404 (no panel leak).
    """
    path = request.scope.get("path") or request.url.path or "/"
    # normalize
    if not path.startswith("/"):
        path = "/" + path

    if _is_public_path(path):
        return await call_next(request)

    prefix = "/" + PANEL_PATH
    if path == prefix or path.startswith(prefix + "/"):
        # strip secret prefix → internal route
        new_path = path[len(prefix):] or "/"
        request.scope["path"] = new_path
        # keep raw path for debugging if needed
        request.state.panel_path = PANEL_PATH
        return await call_next(request)

    # Classic paths without secret → hide panel
    return HTMLResponse(
        "<!DOCTYPE html><html><head><meta charset=utf-8><title>404</title></head>"
        "<body style='font-family:sans-serif;background:#0a0a0f;color:#94a3b8;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center'>"
        "<p>404 — Not Found</p></body></html>",
        status_code=404,
    )


logger.info("=" * 60)
logger.info("PANEL LOGIN: /%s/login", PANEL_PATH)
logger.info("Set env PANEL_PATH to customize, or change in Settings")
logger.info("=" * 60)


# ============================================================
# LOCKS
# ============================================================

SAVE_LOCK = asyncio.Lock()
LINKS_LOCK = asyncio.Lock()
SUBS_LOCK = asyncio.Lock()
SESSIONS_LOCK = asyncio.Lock()


# ============================================================
# SECRET
# ============================================================

def load_or_create_secret() -> str:
    env_secret = os.environ.get("SECRET_KEY")

    if env_secret:
        return env_secret

    try:
        if SECRET_FILE.exists():
            existing = (
                SECRET_FILE
                .read_text(
                    encoding="utf-8"
                )
                .strip()
            )

            if existing:
                return existing

        generated = secrets.token_urlsafe(48)

        SECRET_FILE.write_text(
            generated,
            encoding="utf-8",
        )

        return generated

    except Exception as exc:
        logger.warning(
            "Could not persist SECRET_KEY: %s",
            exc,
        )

        return secrets.token_urlsafe(48)


SECRET_KEY = load_or_create_secret()


# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    "port": PORT,
    "secret": SECRET_KEY,
    "host": os.environ.get(
        "RAILWAY_PUBLIC_DOMAIN",
        "localhost",
    ),
}


# ============================================================
# STATE
# ============================================================

LINKS: dict = {}
SUBS: dict = {}
SESSIONS: dict = {}
connections: dict = {}
CATEGORIES: dict = {}

stats = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": time.time(),
}

error_logs = deque(maxlen=100)
activity_logs = deque(maxlen=250)

hourly_traffic = defaultdict(int)

http_client: httpx.AsyncClient | None = None


# ============================================================
# PROTOCOL
# ============================================================

PROTOCOLS = (
    "vless-ws",
    "xhttp-packet-up",
    "xhttp-stream-up",
    "xhttp-stream-one",
    "vmess-ws",
    "trojan-ws",
    "shadowsocks",
    "socks5",
    "http",
    "hysteria2",
    "tuic",
    "wireguard",
    "highspeed-demo",
    "gaming-lite-demo",
)

PROTOCOL_LABELS = {
    "vless-ws": "VLESS WebSocket ⭐",
    "xhttp-packet-up": "XHTTP Packet Up",
    "xhttp-stream-up": "XHTTP Stream Up",
    "xhttp-stream-one": "XHTTP Stream One",
    "vmess-ws": "VMess WebSocket",
    "trojan-ws": "Trojan WebSocket",
    "shadowsocks": "Shadowsocks",
    "socks5": "SOCKS5",
    "http": "HTTP Proxy",
    "hysteria2": "Hysteria 2",
    "tuic": "TUIC",
    "wireguard": "WireGuard",
    "highspeed-demo": "HighSpeed Upload/Download (دمو)",
    "gaming-lite-demo": "Gaming Lite (دمو)",
}


PROTOCOL_ALIASES = {
    "vmess": "vmess-ws", "trojan": "trojan-ws", "ss": "shadowsocks",
    "socks": "socks5", "hy2": "hysteria2", "hysteria": "hysteria2",
}

DEFAULT_PROTOCOL = "vless-ws"

FINGERPRINTS = (
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "360",
    "qq",
    "random",
    "randomized",
)

DEFAULT_FINGERPRINT = "chrome"

DEFAULT_ALPN_BY_PROTOCOL = {
    "vless-ws": "http/1.1",
    "xhttp-packet-up": "h2,http/1.1",
    "xhttp-stream-up": "h2,http/1.1",
    "xhttp-stream-one": "h2,http/1.1",
}

DEFAULT_PORT = 443

# Public endpoint for configs (Railway domain or TCP Proxy host:port)
# Env: PUBLIC_HOST, PUBLIC_PORT, PUBLIC_SECURITY=tls|none
MTPROTO_CFG = {
    # enabled + list of proxies (up to several); empty list = no telegram proxy on sub page
    "enabled": str(os.environ.get("MTPROTO_ENABLED") or "").strip().lower() in ("1", "true", "yes"),
    "proxies": [],  # [{name, server, port, secret, tag}]
}
# optional single proxy from env for first entry
_mt_srv = (os.environ.get("MTPROTO_SERVER") or "").strip()
_mt_sec = (os.environ.get("MTPROTO_SECRET") or "").strip()
if _mt_srv and _mt_sec:
    try:
        _mt_port = int(os.environ.get("MTPROTO_PORT") or 443)
    except Exception:
        _mt_port = 443
    MTPROTO_CFG["proxies"].append({
        "name": "پروکسی ۱",
        "server": _mt_srv,
        "port": _mt_port,
        "secret": _mt_sec,
        "tag": (os.environ.get("MTPROTO_TAG") or "").strip(),
    })
    MTPROTO_CFG["enabled"] = True


NETWORK_CFG = {
    "public_host": (os.environ.get("PUBLIC_HOST") or "").strip(),
    "public_port": int(os.environ.get("PUBLIC_PORT") or 0) or 0,
    "public_security": (os.environ.get("PUBLIC_SECURITY") or "tls").strip().lower(),  # tls | none
    "prefer_ipv6": str(os.environ.get("PREFER_IPV6") or "1").strip().lower() not in ("0", "false", "no"),
}

MIN_PORT = 1
MAX_PORT = 65535

DEFAULT_SPEED_LIMIT = 0


def normalize_protocol(protocol: str | None) -> str:
    value = str(protocol or DEFAULT_PROTOCOL).strip().lower()
    value = PROTOCOL_ALIASES.get(value, value)
    return value if value in PROTOCOLS else DEFAULT_PROTOCOL


# ============================================================
# LOGGING
# ============================================================

def log_activity(
    kind: str,
    message: str,
    level: str = "info",
):
    activity_logs.append(
        {
            "kind": kind,
            "level": level,
            "message": message,
            "time": datetime.now().isoformat(),
        }
    )


# ============================================================
# HELPERS
# ============================================================

def escape_html(value) -> str:
    return (
        str(
            value
            if value is not None
            else ""
        )
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def safe_int(
    value,
    default=0,
    minimum=0,
    maximum=None,
):
    try:
        number = int(value)
    except Exception:
        number = default

    if number < minimum:
        number = minimum

    if maximum is not None and number > maximum:
        number = maximum

    return number


def safe_float(
    value,
    default=0.0,
    minimum=0.0,
):
    try:
        number = float(value)
    except Exception:
        number = default

    return max(
        minimum,
        number,
    )


def generate_uuid():
    value = secrets.token_hex(16)

    return (
        f"{value[:8]}-"
        f"{value[8:12]}-"
        f"{value[12:16]}-"
        f"{value[16:20]}-"
        f"{value[20:32]}"
    )


def random_config_name(existing=None):
    existing = existing or set()
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(80):
        length = secrets.randbelow(6) + 8
        name = "".join(secrets.choice(alphabet) for _ in range(length))
        if name not in existing and name and not name[0].isdigit():
            return name
    return secrets.token_hex(6)

def sanitize_config_name(name: str) -> str:
    if not name:
        return random_config_name()
    cleaned = "".join(ch for ch in str(name) if ch.isascii() and ch.isalnum())
    if not cleaned or cleaned[0].isdigit():
        cleaned = ("a" + cleaned) if cleaned else random_config_name()
    return cleaned[:40]

def auto_config_name() -> str:
    return random_config_name()


def now_ir():
    if IRAN_TZ:
        return datetime.now(IRAN_TZ)

    return datetime.now()


def uptime():
    seconds = int(
        time.time()
        - stats["start_time"]
    )

    h = seconds // 3600

    m = (
        seconds
        % 3600
    ) // 60

    s = (
        seconds
        % 60
    )

    return (
        f"{h:02d}:"
        f"{m:02d}:"
        f"{s:02d}"
    )


def fmt_bytes(value: int):
    """Always display traffic in gigabytes."""
    value = int(value or 0)
    gb = value / (1024 ** 3)
    if value == 0:
        return "0 GB"
    if gb < 0.01:
        return f"{gb:.4f} GB"
    if gb < 10:
        return f"{gb:.3f} GB"
    return f"{gb:.2f} GB"


def parse_size_to_bytes(
    value: float,
    unit: str,
):
    if value <= 0:
        return 0

    unit = (
        unit
        or "GB"
    ).upper()

    if unit == "TB":
        return int(
            value
            * 1024 ** 4
        )

    if unit == "GB":
        return int(
            value
            * 1024 ** 3
        )

    if unit == "MB":
        return int(
            value
            * 1024 ** 2
        )

    if unit == "KB":
        return int(
            value
            * 1024
        )

    return int(value)


def parse_speed_to_bytes(
    value: float,
    unit: str,
):
    if value <= 0:
        return 0

    unit = (
        unit
        or "MBIT"
    ).upper()

    if unit == "MBIT":
        return int(
            value
            * 1024
            * 1024
            / 8
        )

    if unit == "KB":
        return int(
            value * 1024
        )

    if unit == "MB":
        return int(
            value
            * 1024
            * 1024
        )

    return int(value)


def is_link_expired(
    link: dict,
):
    expiry = link.get(
        "expires_at"
    )

    if not expiry:
        return False

    try:
        return (
            datetime.now()
            > datetime.fromisoformat(
                expiry
            )
        )

    except Exception:
        return False


def sub_used_bytes(sub_id: str | None) -> int:
    """Sum traffic of all configs in a subscription group."""
    if not sub_id:
        return 0
    sub = SUBS.get(sub_id) or {}
    total = 0
    for lid in sub.get("link_ids") or []:
        link = LINKS.get(lid)
        if link:
            total += int(link.get("used_bytes") or 0)
    return total


def sub_limit_bytes(sub_id: str | None) -> int:
    if not sub_id:
        return 0
    sub = SUBS.get(sub_id) or {}
    # explicit group limit
    gl = int(sub.get("limit_bytes") or 0)
    if gl > 0:
        return gl
    # legacy: if every link has the same positive limit, treat as group limit (not sum)
    limits = []
    for lid in sub.get("link_ids") or []:
        link = LINKS.get(lid)
        if not link:
            continue
        limits.append(int(link.get("limit_bytes") or 0))
    if limits and min(limits) > 0 and len(set(limits)) == 1:
        return limits[0]
    return 0


def is_link_allowed(
    link: dict | None,
):
    if link is None:
        return False

    if not link.get(
        "active",
        True,
    ):
        return False

    if is_link_expired(link):
        return False

    limit = int(
        link.get(
            "limit_bytes",
            0,
        )
        or 0
    )

    used = int(
        link.get(
            "used_bytes",
            0,
        )
        or 0
    )

    if (
        limit > 0
        and used >= limit
    ):
        return False

    # group-level quota (shared across all configs in the sub)
    sub_id = link.get("sub_id")
    if sub_id:
        g_limit = sub_limit_bytes(sub_id)
        if g_limit > 0 and sub_used_bytes(sub_id) >= g_limit:
            return False

    return True


def unique_ips_for_uuid(
    uuid: str,
):
    return {
        connection.get("ip")
        for connection in connections.values()
        if connection.get("uuid") == uuid
        and connection.get("ip")
    }


def client_ip(
    request: Request,
):
    forwarded = request.headers.get(
        "x-forwarded-for"
    )

    if forwarded:
        return (
            forwarded
            .split(",")[0]
            .strip()
        )

    real = request.headers.get(
        "x-real-ip"
    )

    if real:
        return real.strip()

    if request.client:
        return request.client.host

    return "unknown"


def is_ip_allowed(
    link: dict | None,
    uuid: str,
    ip: str,
):
    if link is None:
        return False

    limit = int(
        link.get(
            "ip_limit",
            0,
        )
        or 0
    )

    if limit <= 0:
        return True

    ips = unique_ips_for_uuid(uuid)

    if ip in ips:
        return True

    return len(ips) < limit


def get_host(
    request: Request | None = None,
) -> str:

    if request is not None:
        forwarded = request.headers.get(
            "x-forwarded-host"
        )

        normal = request.headers.get(
            "host"
        )

        host = (
            forwarded
            or normal
        )

        if host:
            host = host.split(":")[0].strip()

            CONFIG["host"] = host

            return host

    railway_domain = os.environ.get(
        "RAILWAY_PUBLIC_DOMAIN"
    )

    if railway_domain:
        return railway_domain

    return CONFIG["host"]



def build_one_mtproto_link(server: str, port: int, secret: str) -> dict:
    srv = (server or "").strip()
    sec = (secret or "").strip()
    try:
        prt = int(port or 0) or 443
    except Exception:
        prt = 443
    if not srv or not sec:
        return {"ok": False}
    sec_q = quote(sec, safe="")
    https = f"https://t.me/proxy?server={quote(srv, safe='')}&port={prt}&secret={sec_q}"
    tg = f"tg://proxy?server={quote(srv, safe='')}&port={prt}&secret={sec_q}"
    return {
        "ok": True,
        "server": srv,
        "port": prt,
        "secret": sec,
        "https_link": https,
        "tg_link": tg,
    }


def list_mtproto_proxies() -> list:
    """Normalized list of configured MTProto proxies (0..N)."""
    out = []
    for i, item in enumerate(MTPROTO_CFG.get("proxies") or []):
        if not isinstance(item, dict):
            continue
        srv = str(item.get("server") or "").strip()
        sec = str(item.get("secret") or "").strip()
        if not srv or not sec:
            continue
        try:
            prt = int(item.get("port") or 443)
        except Exception:
            prt = 443
        name = str(item.get("name") or f"پروکسی {i+1}").strip()[:40]
        link = build_one_mtproto_link(srv, prt, sec)
        if not link.get("ok"):
            continue
        out.append({
            "name": name,
            "server": srv,
            "port": prt,
            "secret": sec,
            "tag": str(item.get("tag") or "").strip(),
            "https_link": link["https_link"],
            "tg_link": link["tg_link"],
        })
    return out


def build_mtproto_links(server: str | None = None, port: int | None = None, secret: str | None = None) -> dict:
    """Back-compat: first proxy or explicit args."""
    if server and secret:
        one = build_one_mtproto_link(server, port or 443, secret)
        one["enabled"] = bool(MTPROTO_CFG.get("enabled"))
        return one
    proxies = list_mtproto_proxies()
    if not proxies:
        return {"ok": False, "enabled": bool(MTPROTO_CFG.get("enabled")), "proxies": []}
    first = dict(proxies[0])
    first["ok"] = True
    first["enabled"] = bool(MTPROTO_CFG.get("enabled"))
    first["proxies"] = proxies
    return first


def generate_mtproto_secret(fake_tls_domain: str = "") -> str:
    """16-byte hex secret, or Fake-TLS (ee + 32hex + domain-as-hex)."""
    raw = secrets.token_hex(16)
    domain = (fake_tls_domain or "").strip().lower()
    if not domain:
        return raw
    return "ee" + raw + domain.encode("utf-8").hex()


def get_public_endpoint(request: Request | None = None) -> tuple[str, int, str]:
    """Host, port, security used inside generated client configs.
    Priority: NETWORK_CFG (settings/env) → request host + 443 + tls.
    """
    host = (NETWORK_CFG.get("public_host") or "").strip()
    port = int(NETWORK_CFG.get("public_port") or 0)
    security = (NETWORK_CFG.get("public_security") or "tls").strip().lower()
    if security not in ("tls", "none"):
        security = "tls"
    if not host:
        host = get_host(request)
    if port <= 0:
        port = DEFAULT_PORT
    # strip brackets if user pasted [ipv6]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, port, security


# ============================================================
# PASSWORD
# ============================================================

def hash_password(
    password: str,
) -> str:

    payload = (
        password
        + SECRET_KEY
    ).encode("utf-8")

    return hashlib.sha256(
        payload
    ).hexdigest()


# Password: from ADMIN_PASSWORD env, or set on first login (setup form)
DEFAULT_OWNER_USERNAME = "admin"
_env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
AUTH = {
    "password_hash": hash_password(_env_pw) if _env_pw else "",
    "password_configured": bool(_env_pw),
}

# Sub-admin accounts (panel operators with granular permissions)
ADMIN_ACCOUNTS: dict = {}
# session_token -> {"role": "owner"|"admin", "admin_id": str|None, "username": str}
SESSION_META: dict = {}

ALL_PERMS = (
    "dash", "configs", "create", "stats", "logs",
    "settings", "support", "telegram", "news", "admins",
)
DEFAULT_PERMS = {p: True for p in ALL_PERMS}


def default_admin_record(username: str, password: str, **kwargs) -> dict:
    return {
        "id": secrets.token_hex(8),
        "username": username.strip().lower(),
        "password_hash": hash_password(password),
        "label": kwargs.get("label") or username,
        "limit_bytes": int(kwargs.get("limit_bytes") or 0),
        "used_bytes": 0,
        "expires_at": kwargs.get("expires_at"),
        "active": True,
        "blocked": False,
        "permissions": {**DEFAULT_PERMS, **(kwargs.get("permissions") or {})},
        "created_at": datetime.now().isoformat(),
    }


def find_admin_by_username(username: str):
    u = (username or "").strip().lower()
    for aid, a in ADMIN_ACCOUNTS.items():
        if a.get("username") == u:
            return aid, a
    return None, None


def admin_is_valid(admin: dict) -> bool:
    if not admin or admin.get("blocked") or not admin.get("active", True):
        return False
    exp = admin.get("expires_at")
    if exp:
        try:
            if datetime.now() > datetime.fromisoformat(str(exp)):
                return False
        except Exception:
            pass
    limit = int(admin.get("limit_bytes") or 0)
    used = int(admin.get("used_bytes") or 0)
    if limit > 0 and used >= limit:
        return False
    return True



# ============================================================
# LOGIN BRUTE-FORCE PROTECTION
# ============================================================
# Maximum failed login attempts per IP inside the rolling window.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_SECONDS = 30 * 60  # 30 minutes lockout
LOGIN_MIN_PASSWORD_LENGTH = 6

LOGIN_FAILURES = defaultdict(deque)
LOGIN_LOCKED_UNTIL = {}


def _cleanup_login_state(ip: str, now: float | None = None):
    now = now if now is not None else time.time()

    locked_until = LOGIN_LOCKED_UNTIL.get(ip, 0)
    if locked_until and locked_until <= now:
        LOGIN_LOCKED_UNTIL.pop(ip, None)

    failures = LOGIN_FAILURES.get(ip)
    if not failures:
        return

    cutoff = now - LOGIN_WINDOW_SECONDS
    while failures and failures[0] <= cutoff:
        failures.popleft()

    if not failures:
        LOGIN_FAILURES.pop(ip, None)


def login_is_blocked(ip: str):
    now = time.time()
    _cleanup_login_state(ip, now)

    locked_until = LOGIN_LOCKED_UNTIL.get(ip, 0)
    if locked_until > now:
        return True, max(1, int(locked_until - now))

    return False, 0


def register_login_failure(ip: str):
    now = time.time()
    _cleanup_login_state(ip, now)

    failures = LOGIN_FAILURES.setdefault(ip, deque())
    failures.append(now)

    if len(failures) >= LOGIN_MAX_ATTEMPTS:
        LOGIN_LOCKED_UNTIL[ip] = now + LOGIN_LOCKOUT_SECONDS
        failures.clear()
        log_activity(
            "auth",
            f"IP به دلیل تلاش‌های متعدد ورود ناموفق به مدت {LOGIN_LOCKOUT_SECONDS // 60} دقیقه مسدود شد: {ip}",
            "err",
        )
        return True, LOGIN_LOCKOUT_SECONDS

    return False, max(0, LOGIN_MAX_ATTEMPTS - len(failures))


def clear_login_failures(ip: str):
    LOGIN_FAILURES.pop(ip, None)
    LOGIN_LOCKED_UNTIL.pop(ip, None)


# ============================================================
# SESSION
# ============================================================

SESSION_COOKIE = "panel_session"

SESSION_TTL = (
    60
    * 60
    * 24
    * 365
)


async def create_session(meta: dict | None = None) -> str:

    token = secrets.token_urlsafe(48)

    async with SESSIONS_LOCK:
        SESSIONS[token] = (
            time.time()
            + SESSION_TTL
        )
        SESSION_META[token] = meta or {"role": "owner", "admin_id": None, "username": "owner"}

    return token


async def is_valid_session(
    token: str | None,
) -> bool:

    if not token:
        return False

    async with SESSIONS_LOCK:

        expiry = SESSIONS.get(token)

        if expiry is None:
            return False

        if expiry < time.time():

            SESSIONS.pop(
                token,
                None,
            )

            return False

        return True


async def destroy_session(
    token: str | None,
):
    if not token:
        return

    async with SESSIONS_LOCK:
        SESSIONS.pop(
            token,
            None,
        )
        SESSION_META.pop(token, None)


def get_session_meta(token: str | None) -> dict:
    if not token:
        return {"role": "owner", "admin_id": None, "username": "owner", "permissions": {p: True for p in ALL_PERMS}}
    meta = dict(SESSION_META.get(token) or {"role": "owner", "admin_id": None, "username": "owner"})
    if meta.get("role") == "owner":
        meta["permissions"] = {p: True for p in ALL_PERMS}
    else:
        aid = meta.get("admin_id")
        admin = ADMIN_ACCOUNTS.get(aid or "") or {}
        meta["permissions"] = {p: bool((admin.get("permissions") or {}).get(p, False)) for p in ALL_PERMS}
        meta["blocked"] = bool(admin.get("blocked"))
    return meta


def require_perm(perm: str):
    async def _dep(request: Request, token=Depends(require_auth)):
        meta = get_session_meta(token)
        if meta.get("role") == "owner":
            return token
        if not (meta.get("permissions") or {}).get(perm):
            raise HTTPException(status_code=403, detail="دسترسی به این بخش مجاز نیست")
        return token
    return _dep


async def require_auth(
    request: Request,
):
    token = request.cookies.get(
        SESSION_COOKIE
    )

    if not await is_valid_session(
        token
    ):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
        )

    meta = get_session_meta(token)
    if meta.get("role") == "admin":
        aid = meta.get("admin_id")
        admin = ADMIN_ACCOUNTS.get(aid or "")
        if not admin_is_valid(admin or {}):
            await destroy_session(token)
            raise HTTPException(status_code=401, detail="حساب منقضی یا مسدود شده است")

    return token


def set_auth_cookie(
    response,
    request: Request,
    token: str,
):
    forwarded_proto = (
        request.headers
        .get(
            "x-forwarded-proto",
            "",
        )
        .lower()
    )

    is_https = (
        forwarded_proto == "https"
        or request.url.scheme == "https"
    )

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
        secure=is_https,
    )


# ============================================================
# VLESS LINK GENERATION
# ============================================================

def generate_vless_link(
    uuid: str, host: str, remark: str = "Panel",
    protocol: str = DEFAULT_PROTOCOL, fingerprint: str | None = None,
    alpn: str | None = None, port: int | None = None,
    security: str | None = None,
):
    protocol = normalize_protocol(protocol)
    fp = (fingerprint or DEFAULT_FINGERPRINT).strip().lower()
    if fp not in FINGERPRINTS: fp = DEFAULT_FINGERPRINT
    port_value = safe_int(port, DEFAULT_PORT, MIN_PORT, MAX_PORT)
    alpn_value = (alpn or DEFAULT_ALPN_BY_PROTOCOL.get(protocol, "http/1.1")).strip()
    label = quote(str(remark or "Panel"), safe="")
    sec = (security or NETWORK_CFG.get("public_security") or "tls").strip().lower()
    if sec not in ("tls", "none"):
        sec = "tls"
    # host may be IPv6 — bracket in URL userinfo@host:port
    host_url = host
    if ":" in host and not host.startswith("["):
        host_url = f"[{host}]"
    if protocol == "vless-ws":
        q = {
            "encryption": "none",
            "security": sec,
            "type": "ws",
            "host": host,
            "path": f"/ws/{uuid}",
            "fp": fp,
            "packetEncoding": "xudp",
        }
        if sec == "tls":
            q["sni"] = host
            q["alpn"] = alpn_value or "http/1.1"
        return "vless://" + uuid + "@" + host_url + ":" + str(port_value) + "?" + "&".join(f"{k}={quote(str(v), safe=',/') }" for k,v in q.items()) + "#" + label
    if protocol.startswith("xhttp-"):
        mode = protocol.replace("xhttp-", "")
        q = {
            "encryption": "none",
            "security": sec,
            "type": "xhttp",
            "mode": mode,
            "host": host,
            "path": f"/xhttp-siz10/{mode}/{uuid}",
            "fp": fp,
            "packetEncoding": "xudp",
        }
        if sec == "tls":
            q["sni"] = host
            q["alpn"] = alpn_value or "h2,http/1.1"
        return "vless://" + uuid + "@" + host_url + ":" + str(port_value) + "?" + "&".join(f"{k}={quote(str(v), safe=',/') }" for k,v in q.items()) + "#" + label
    if protocol == "vmess-ws":
        raw = {"v":"2","ps":remark,"add":host,"port":port_value,"id":uuid,"aid":0,"scy":"auto","net":"ws","type":"none","host":host,"path":f"/ws/{uuid}","tls":"tls","sni":host,"fp":fp}
        return "vmess://" + base64.b64encode(json.dumps(raw,separators=(",",":"),ensure_ascii=False).encode()).decode()
    if protocol == "trojan-ws":
        return f"trojan://{uuid}@{host_url}:{port_value}?security=tls&type=ws&host={quote(host)}&path={quote('/ws/'+uuid)}&sni={quote(host)}#{label}"
    if protocol == "shadowsocks":
        method = os.getenv("SS_METHOD", "aes-256-gcm")
        userinfo = base64.urlsafe_b64encode(f"{method}:{uuid}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{host_url}:{port_value}#{label}"
    if protocol == "socks5": return f"socks5://{uuid}:{uuid}@{host}:{port_value}#{label}"
    if protocol == "http": return f"http://{uuid}:{uuid}@{host}:{port_value}#{label}"
    if protocol == "hysteria2": return f"hysteria2://{uuid}@{host_url}:{port_value}/?sni={quote(host)}&insecure=0#{label}"
    if protocol == "tuic": return f"tuic://{uuid}:{uuid}@{host_url}:{port_value}?sni={quote(host)}&alpn=h3#{label}"
    if protocol == "wireguard": return f"wireguard://{uuid}@{host}:{port_value}?publicKey={uuid}#{label}"
    if protocol == "highspeed-demo":
        q = {"encryption":"none","security":"tls","type":"xhttp","mode":"stream-up","host":host,"path":f"/xhttp-siz10/stream-up/{uuid}","sni":host,"fp":fp,"alpn":"h2,http/1.1"}
        return "vless://" + uuid + "@" + host + ":" + str(port_value) + "?" + "&".join(f"{k}={quote(str(v), safe=',/')}" for k,v in q.items()) + "#" + label
    if protocol == "gaming-lite-demo":
        return f"hysteria2://{uuid}@{host}:{port_value}/?sni={quote(host)}&insecure=0&obfs=salamander#{label}"
    return f"vless://{uuid}@{host}:{port_value}"


def resolve_link_endpoint(link: dict | None, request: Request | None = None) -> tuple[str, int, str]:
    host, port, sec = get_public_endpoint(request)
    if link:
        # per-link override if stored
        if link.get("endpoint_host"):
            host = str(link["endpoint_host"]).strip() or host
        if link.get("port"):
            try:
                p = int(link.get("port") or 0)
                if p > 0:
                    port = p
            except Exception:
                pass
        if link.get("security") in ("tls", "none"):
            sec = link["security"]
    return host, port, sec

def vless_link_for_link(
    link: dict,
    uid: str,
    host: str,
    request: Request | None = None,
):
    h, port, sec = resolve_link_endpoint(link, request)
    # per-link endpoint wins; else caller host if no global public_host
    if link and (link.get("endpoint_host") or "").strip():
        h = str(link.get("endpoint_host")).strip()
        if link.get("port"):
            try:
                port = int(link["port"]) or port
            except Exception:
                pass
        if link.get("security") in ("tls", "none"):
            sec = link["security"]
    elif host and not (NETWORK_CFG.get("public_host") or "").strip():
        h = host
    return generate_vless_link(
        uid,
        h,
        remark=str(link.get("label") or "Config"),
        protocol=link.get(
            "protocol",
            DEFAULT_PROTOCOL,
        ),
        fingerprint=link.get(
            "fingerprint",
            DEFAULT_FINGERPRINT,
        ),
        alpn=link.get("alpn"),
        port=port,
        security=sec,
    )


def get_link_info(
    link: dict,
    uid: str,
    host: str,
):
    connected_count = len(unique_ips_for_uuid(uid))
    is_active = is_link_allowed(link)
    limit_b = int(link.get("limit_bytes", 0) or 0)
    used_b = int(link.get("used_bytes", 0) or 0)
    is_expired = is_link_expired(link) or (limit_b > 0 and used_b >= limit_b)
    if not is_active or is_expired:
        status_color = "red"
    elif connected_count > 0:
        status_color = "green"
    else:
        status_color = "gray"
    clean_ips = link.get("clean_ips") or []
    cfg_count = int(link.get("config_count") or 1)
    show_vless = len(clean_ips) <= 1 and cfg_count <= 1
    cat = CATEGORIES.get(str(link.get("category_id") or "0")) or {}
    return {
        "uuid": uid,
        "name": link.get("label", ""),
        "label": link.get("label", ""),
        "protocol": link.get("protocol", DEFAULT_PROTOCOL),
        "active": is_active,
        "used_bytes": used_b,
        "limit_bytes": limit_b,
        "expires_at": link.get("expires_at"),
        "ip_limit": int(link.get("ip_limit", 0) or 0),
        "speed_limit_bytes": int(link.get("speed_limit_bytes", 0) or 0),
        "connection_limit": int(link.get("connection_limit", 0) or 0),
        "fragment": link.get("fragment", "off"),
        "fingerprint": link.get("fingerprint", DEFAULT_FINGERPRINT),
        "alpn": link.get("alpn", ""),
        "port": link.get("port", DEFAULT_PORT),
        "note": link.get("note", ""),
        "clean_ips": clean_ips,
        "alarm_enabled": bool(link.get("alarm_enabled", False)),
        "category_id": str(link.get("category_id") or "0"),
        "sort_order": int(link.get("sort_order") or 0),
        "category_number": int(cat.get("number", 0)),
        "category_name": str(cat.get("name", "عمومی")),
        "config_count": cfg_count,
        "status_color": status_color,
        "connected_ips": connected_count,
        "show_vless": show_vless,
        "vless": vless_link_for_link(link, uid, host) if show_vless else "",
        "vless_full": vless_link_for_link(link, uid, host),
        "sub": f"https://{host}/sub/{uid}",
        "info": f"https://{host}/info/{uid}",
        "support": SUPPORT_USERNAME,
    }


# ============================================================
# PERSISTENCE
# ============================================================

async def load_state():

    global AUTH

    try:

        DATA_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not DATA_FILE.exists():
            return

        async with aiofiles.open(
            DATA_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            raw = await file.read()

        data = json.loads(raw)

        LINKS.update(
            data.get(
                "links",
                {},
            )
        )

        SUBS.update(
            data.get(
                "subs",
                {},
            )
        )

        CATEGORIES.update(
            data.get(
                "categories",
                {},
            )
        )

        ADMIN_ACCOUNTS.clear()
        ADMIN_ACCOUNTS.update(data.get("admin_accounts") or {})

        net = data.get("network") or {}
        if isinstance(net, dict):
            if net.get("public_host") is not None:
                NETWORK_CFG["public_host"] = str(net.get("public_host") or "").strip()
            if net.get("public_port") is not None:
                try:
                    NETWORK_CFG["public_port"] = int(net.get("public_port") or 0)
                except Exception:
                    pass
            if net.get("public_security") in ("tls", "none"):
                NETWORK_CFG["public_security"] = net["public_security"]
            if "prefer_ipv6" in net:
                NETWORK_CFG["prefer_ipv6"] = bool(net.get("prefer_ipv6"))

        mt = data.get("mtproto") or {}
        if isinstance(mt, dict):
            if "enabled" in mt:
                MTPROTO_CFG["enabled"] = bool(mt.get("enabled"))
            prox = mt.get("proxies")
            if isinstance(prox, list):
                cleaned = []
                for item in prox[:8]:
                    if not isinstance(item, dict):
                        continue
                    srv = str(item.get("server") or "").strip()
                    sec = str(item.get("secret") or "").strip()
                    if not srv or not sec:
                        continue
                    try:
                        prt = int(item.get("port") or 443)
                    except Exception:
                        prt = 443
                    cleaned.append({
                        "name": str(item.get("name") or f"پروکسی {len(cleaned)+1}")[:40],
                        "server": srv,
                        "port": prt,
                        "secret": sec,
                        "tag": str(item.get("tag") or "").strip(),
                    })
                MTPROTO_CFG["proxies"] = cleaned
            elif mt.get("server") and mt.get("secret"):
                # migrate old single-proxy shape
                try:
                    prt = int(mt.get("port") or 443)
                except Exception:
                    prt = 443
                MTPROTO_CFG["proxies"] = [{
                    "name": "پروکسی ۱",
                    "server": str(mt.get("server") or "").strip(),
                    "port": prt,
                    "secret": str(mt.get("secret") or "").strip(),
                    "tag": str(mt.get("tag") or "").strip(),
                }]

        stored_password = data.get(
            "password_hash"
        )

        # RESET_PASSWORD=1 → clear stored password and show setup form again
        if str(os.environ.get("RESET_PASSWORD", "")).strip() in ("1", "true", "yes"):
            AUTH["password_hash"] = ""
            AUTH["password_configured"] = False
            data["password_hash"] = ""
            logger.warning("RESET_PASSWORD set: owner password cleared, setup required")
        elif stored_password and str(stored_password).strip():
            AUTH["password_hash"] = stored_password
            AUTH["password_configured"] = True
        elif stored_password is not None and not str(stored_password).strip():
            AUTH["password_hash"] = ""
            AUTH["password_configured"] = False
        elif os.environ.get("ADMIN_PASSWORD", "").strip():
            AUTH["password_hash"] = hash_password(os.environ.get("ADMIN_PASSWORD", "").strip())
            AUTH["password_configured"] = True
        # else: leave unconfigured → first-login setup form

        # Compatibility for older records
        for uid, link in LINKS.items():

            link.setdefault(
                "protocol",
                DEFAULT_PROTOCOL,
            )

            link.setdefault(
                "fingerprint",
                DEFAULT_FINGERPRINT,
            )

            link.setdefault(
                "alpn",
                "",
            )

            link.setdefault(
                "port",
                DEFAULT_PORT,
            )

            link.setdefault(
                "ip_limit",
                0,
            )

            link.setdefault(
                "speed_limit_bytes",
                0,
            )

            link.setdefault(
                "connection_limit",
                0,
            )

            link.setdefault(
                "fragment",
                "off",
            )

            link.setdefault(
                "used_bytes",
                0,
            )
            link.setdefault("clean_ips", [])
            link.setdefault("alarm_enabled", False)
            link.setdefault("category_id", "0")
            link.setdefault("config_count", 1)
            link.setdefault("sort_order", 0)
            link.setdefault("usage_history", [])

        logger.info(
            "State loaded: %d links / %d subscriptions",
            len(LINKS),
            len(SUBS),
        )

    except Exception as exc:

        logger.exception(
            "Could not load state: %s",
            exc,
        )


async def save_state():

    async with SAVE_LOCK:

        try:

            DATA_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            payload = {
                "links":
                    dict(LINKS),

                "subs":
                    dict(SUBS),

                "categories":
                    dict(CATEGORIES),

                "admin_accounts":
                    dict(ADMIN_ACCOUNTS),

                "password_hash":
                    AUTH[
                        "password_hash"
                    ],

                "network":
                    dict(NETWORK_CFG),

                "mtproto":
                    dict(MTPROTO_CFG),

                "saved_at":
                    datetime.now().isoformat(),
            }

            temp_file = (
                DATA_FILE.with_suffix(
                    ".tmp"
                )
            )

            async with aiofiles.open(
                temp_file,
                "w",
                encoding="utf-8",
            ) as file:

                await file.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                )

            temp_file.replace(
                DATA_FILE
            )

        except Exception as exc:

            logger.exception(
                "Could not save state: %s",
                exc,
            )


# ============================================================
# DEFAULT LINK
# ============================================================

_default_link_created = False



async def ensure_default_categories():
    # گروه‌های پیش‌فرض ساخته نمی‌شوند — کاربر خودش می‌سازد
    return


async def ensure_default_link():

    global _default_link_created

    if _default_link_created:
        return

    async with LINKS_LOCK:

        if not any(
            item.get("is_default")
            for item in LINKS.values()
        ):

            digest = hashlib.sha256(
                (
                    "default"
                    + SECRET_KEY
                ).encode("utf-8")
            ).hexdigest()

            uid = (
                f"{digest[:8]}-"
                f"{digest[8:12]}-"
                f"{digest[12:16]}-"
                f"{digest[16:20]}-"
                f"{digest[20:32]}"
            )

            LINKS[uid] = {
                "label":
                    "لینک پیش‌فرض",

                "limit_bytes":
                    0,

                "used_bytes":
                    0,

                "created_at":
                    datetime.now().isoformat(),

                "active":
                    True,

                "expires_at":
                    None,

                "note":
                    "",

                "is_default":
                    True,

                "sub_id":
                    None,

                "protocol":
                    DEFAULT_PROTOCOL,

                "fingerprint":
                    DEFAULT_FINGERPRINT,

                "alpn":
                    "http/1.1",

                "port":
                    DEFAULT_PORT,

                "ip_limit":
                    0,

                "speed_limit_bytes":
                    DEFAULT_SPEED_LIMIT,

                "connection_limit":
                    0,

                "fragment":
                    "off",
            }

            asyncio.create_task(
                save_state()
            )

    _default_link_created = True


# ============================================================
# LINK MANAGEMENT
# ============================================================

async def make_link(
    label: str = "لینک جدید",
    limit_bytes: int = 0,
    expires_at: str | None = None,
    note: str = "",
    sub_id: str | None = None,
    protocol: str = DEFAULT_PROTOCOL,
    fingerprint: str = DEFAULT_FINGERPRINT,
    alpn: str = "",
    port: int = DEFAULT_PORT,
    ip_limit: int = 0,
    speed_limit_bytes: int = 0,
    connection_limit: int = 0,
    fragment: str = "off",
    clean_ips=None,
    alarm_enabled: bool = False,
    category_id: str = "0",
    config_count: int = 1,
    endpoint_host: str = "",
    security: str = "",
):

    protocol = normalize_protocol(protocol)

    fingerprint = (
        fingerprint
        or DEFAULT_FINGERPRINT
    ).strip().lower()

    if fingerprint not in FINGERPRINTS:
        fingerprint = DEFAULT_FINGERPRINT

    if not (
        MIN_PORT
        <= port
        <= MAX_PORT
    ):
        port = DEFAULT_PORT

    uid = generate_uuid()

    record = {
        "label":
            sanitize_config_name((label or "").strip() or random_config_name()),

        "limit_bytes":
            max(
                0,
                int(limit_bytes),
            ),

        "used_bytes":
            0,

        "created_at":
            datetime.now().isoformat(),

        "active":
            True,

        "expires_at":
            expires_at,

        "note":
            (
                note
                or ""
            ).strip()[:500],

        "is_default":
            False,

        "sub_id":
            sub_id,

        "protocol":
            protocol,

        "fingerprint":
            fingerprint,

        "alpn":
            (
                alpn
                or ""
            ).strip()[:100],

        "port":
            port,

        "ip_limit":
            max(
                0,
                int(ip_limit),
            ),

        "speed_limit_bytes":
            max(
                0,
                int(speed_limit_bytes),
            ),

        "connection_limit":
            max(
                0,
                int(connection_limit),
            ),

        "fragment":
            (
                fragment
                or "off"
            ).strip().lower(),

        "security_profile": "balanced",
        "multi_login": False,
        "protocol_label": PROTOCOL_LABELS.get(protocol, protocol),
        "clean_ips": list(clean_ips or []),
        "alarm_enabled": bool(alarm_enabled),
        "category_id": str(category_id or "0"),
        "config_count": max(1, min(40, int(config_count or 1))),
        "usage_history": [],
        "endpoint_host": (endpoint_host or "").strip(),
        "security": (security or "").strip() if (security or "").strip() in ("tls", "none") else "",
    }

    async with LINKS_LOCK:
        LINKS[uid] = record

    if sub_id:

        async with SUBS_LOCK:

            if sub_id in SUBS:

                ids = SUBS[
                    sub_id
                ].setdefault(
                    "link_ids",
                    [],
                )

                if uid not in ids:
                    ids.append(uid)

    await save_state()

    log_activity(
        "link",
        (
            f"کانفیگ "
            f"«{record['label']}» "
            f"ساخته شد"
        ),
        "ok",
    )

    return uid, record


async def remove_link(
    uid: str,
):

    async with LINKS_LOCK:

        if uid not in LINKS:
            return None

        label = LINKS[
            uid
        ].get(
            "label",
            uid,
        )

        sub_id = LINKS[
            uid
        ].get(
            "sub_id"
        )

        del LINKS[uid]

    if sub_id:

        async with SUBS_LOCK:

            if sub_id in SUBS:

                ids = SUBS[
                    sub_id
                ].get(
                    "link_ids",
                    [],
                )

                if uid in ids:
                    ids.remove(uid)

    await save_state()

    log_activity(
        "link",
        (
            f"کانفیگ "
            f"«{label}» "
            f"حذف شد"
        ),
        "warn",
    )

    return label


async def set_link_active(
    uid: str,
    active: bool,
):

    async with LINKS_LOCK:

        if uid not in LINKS:
            return None

        LINKS[
            uid
        ][
            "active"
        ] = bool(active)

        record = LINKS[uid]

    await save_state()

    log_activity(
        "link",
        (
            f"کانفیگ "
            f"«{record['label']}» "
            f"{'فعال' if active else 'غیرفعال'} شد"
        ),
        "ok"
        if active
        else "warn",
    )

    return record


# ============================================================
# SUB GROUPS
# ============================================================

async def create_sub_group(
    name: str = "گروه جدید",
    desc: str = "",
    password: str = "",
):

    name = (
        name
        or "گروه جدید"
    ).strip()[:60]

    desc = (
        desc
        or ""
    ).strip()[:200]

    password = (
        password
        or ""
    ).strip()

    sub_id = generate_uuid()

    uuid_key = secrets.token_urlsafe(16)

    record = {
        "name":
            name,

        "desc":
            desc,

        "password_hash":
            (
                hash_password(password)
                if password
                else None
            ),

        "uuid_key":
            uuid_key,

        "created_at":
            datetime.now().isoformat(),

        "link_ids":
            [],

        "limit_bytes": 0,
    }

    async with SUBS_LOCK:
        SUBS[sub_id] = record

    await save_state()

    log_activity(
        "sub",
        (
            f"گروه "
            f"«{name}» "
            f"ساخته شد"
        ),
        "ok",
    )

    return (
        sub_id,
        record,
    )


async def set_link_sub(
    uid: str,
    sub_id: str | None,
):

    async with LINKS_LOCK:

        if uid not in LINKS:
            return False

        old_sub = LINKS[
            uid
        ].get(
            "sub_id"
        )

        label = LINKS[
            uid
        ].get(
            "label",
            uid,
        )

    if sub_id is not None:

        async with SUBS_LOCK:

            if sub_id not in SUBS:
                return False

    async with SUBS_LOCK:

        if (
            old_sub
            and old_sub in SUBS
        ):

            ids = SUBS[
                old_sub
            ].get(
                "link_ids",
                [],
            )

            if uid in ids:
                ids.remove(uid)

        if (
            sub_id
            and sub_id in SUBS
        ):

            ids = SUBS[
                sub_id
            ].setdefault(
                "link_ids",
                [],
            )

            if uid not in ids:
                ids.append(uid)

    async with LINKS_LOCK:

        if uid in LINKS:

            LINKS[
                uid
            ][
                "sub_id"
            ] = sub_id

    await save_state()

    log_activity(
        "link",
        (
            f"کانفیگ "
            f"«{label}» "
            f"{'به گروه اضافه شد' if sub_id else 'از گروه خارج شد'}"
        ),
        "info",
    )

    return True


async def remove_sub_group(
    sub_id: str,
):

    async with SUBS_LOCK:

        if sub_id not in SUBS:
            return None

        name = SUBS[
            sub_id
        ].get(
            "name",
            sub_id,
        )

        del SUBS[sub_id]

    async with LINKS_LOCK:

        for link in LINKS.values():

            if (
                link.get("sub_id")
                == sub_id
            ):
                link["sub_id"] = None

    await save_state()

    log_activity(
        "sub",
        (
            f"گروه "
            f"«{name}» "
            f"حذف شد"
        ),
        "warn",
    )

    return name


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    global http_client

    limits = httpx.Limits(
        max_connections=500,
        max_keepalive_connections=100,
    )

    timeout = httpx.Timeout(
        30.0,
        connect=10.0,
    )

    http_client = httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=True,
    )

    await load_state()
    if str(os.environ.get("RESET_PASSWORD", "")).strip() in ("1", "true", "yes"):
        try:
            await save_state()
        except Exception as exc:
            logger.warning("could not persist password reset: %s", exc)

    await ensure_default_categories()
    await ensure_default_link()

    log_activity(
        "system",
        (
            f"{APP_NAME} "
            f"v{APP_VERSION} "
            f"راه‌اندازی شد"
        ),
        "ok",
    )

    logger.info(
        "%s v%s started on 0.0.0.0:%s",
        APP_NAME,
        APP_VERSION,
        PORT,
    )

    logger.info(
        "Data directory: %s",
        DATA_DIR,
    )


@app.on_event("shutdown")
async def shutdown():

    await save_state()

    if http_client:
        await http_client.aclose()


# ============================================================
# LANDING
# ============================================================

LANDING_HTML = r"""
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>AGN021G</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>

<link
href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;600;700;800;900&display=swap"
rel="stylesheet">

<style>
*{
    box-sizing:border-box;
}

html,body{
    margin:0;
    min-height:100%;
}

body{
    min-height:100vh;
    display:flex;
    justify-content:center;
    align-items:center;
    padding:20px;
    color:#fff;
    font-family:"Vazirmatn",sans-serif;

    background:
        radial-gradient(
            circle at 15% 15%,
            rgba(37,99,235,.22),
            transparent 30%
        ),
        radial-gradient(
            circle at 85% 85%,
            rgba(59,130,246,.18),
            transparent 30%
        ),
        #07070a;
}

.card{
    width:100%;
    max-width:580px;
    padding:32px;
    border-radius:28px;

    border:1px solid rgba(255,255,255,.09);

    background:
        linear-gradient(
            145deg,
            rgba(255,255,255,.07),
            rgba(255,255,255,.025)
        );

    backdrop-filter:blur(28px) saturate(150%);

    box-shadow:
        0 30px 90px rgba(0,0,0,.45);
}

.brand{
    display:flex;
    align-items:center;
    gap:12px;
}

.logo{
    width:48px;
    height:48px;
    border-radius:15px;

    display:flex;
    justify-content:center;
    align-items:center;

    font-size:18px;
    font-weight:900;

    background:
        linear-gradient(
            135deg,
            #2563eb,
            #3b82f6
        );
}

.brand-name{
    font-size:17px;
    font-weight:900;
}

.version{
    margin-top:4px;
    font-size:11px;
    color:#60a5fa;
}

.status{
    display:inline-block;
    margin-top:23px;
    padding:7px 11px;
    border-radius:999px;

    color:#86efac;
    background:rgba(34,197,94,.07);
    border:1px solid rgba(34,197,94,.15);

    font-size:11px;
}

h1{
    margin:18px 0 0;
    font-size:28px;
    line-height:1.55;
}

.desc{
    margin-top:12px;
    color:rgba(255,255,255,.52);
    line-height:2;
    font-size:13px;
}

.path{
    margin-top:22px;
    padding:15px;
    border-radius:15px;

    background:rgba(0,0,0,.18);
    border:1px solid rgba(255,255,255,.07);

    direction:ltr;
    text-align:left;
    font-family:Consolas,monospace;
    color:#93c5fd;
}

.actions{
    display:flex;
    gap:10px;
    margin-top:20px;
}

.btn{
    flex:1;
    padding:13px;
    border-radius:14px;
    text-align:center;
    text-decoration:none;

    font-size:12px;
    font-weight:800;
}

.primary{
    color:#fff;
    background:
        linear-gradient(
            135deg,
            #2563eb,
            #3b82f6
        );
}

.secondary{
    color:#fff;
    background:rgba(255,255,255,.035);
    border:1px solid rgba(255,255,255,.08);
}

.footer{
    margin-top:22px;
    padding-top:16px;
    border-top:1px solid rgba(255,255,255,.07);

    display:flex;
    justify-content:space-between;

    font-size:10px;
    color:rgba(255,255,255,.35);
}

.support{
    color:#60a5fa;
    text-decoration:none;
}

@media(max-width:600px){
    .card{
        padding:24px;
        border-radius:22px;
    }

    h1{
        font-size:23px;
    }

    .actions{
        flex-direction:column;
    }
}

/* responsive system */
html{scroll-behavior:smooth} body{overflow-x:hidden} button,input,select,textarea{touch-action:manipulation} .modal{overscroll-behavior:contain}
@media(max-width:900px){.container,.shell,.dashboard,.main,.content{max-width:100%!important;width:100%!important}.grid,.stats-grid,.cards-grid,.form-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important}.sidebar{z-index:1000}}
@media(max-width:640px){body{padding:10px!important;font-size:14px}.grid,.stats-grid,.cards-grid,.form-grid{grid-template-columns:1fr!important}.card,.panel,.section,.modal{border-radius:18px!important}.modal{max-height:92vh;overflow:auto;padding:14px!important}.header,.topbar,.toolbar,.actions{flex-wrap:wrap!important}.header>* ,.topbar>*{max-width:100%}.btn,button{min-height:44px}.field input,.field select,.field textarea,input,select,textarea{min-height:44px;font-size:16px;max-width:100%}table{display:block;overflow-x:auto;white-space:nowrap}.link-row,.config-row{flex-direction:column!important;align-items:stretch!important}.brand-name{font-size:15px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}

/* Toggle switch */
.switch{position:relative;display:inline-block;width:42px;height:24px;vertical-align:middle}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:rgba(255,255,255,.12);border-radius:24px;transition:.2s}
.slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:var(--green)}
.switch input:checked+.slider:before{transform:translateX(18px)}


.conn-badge{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:20px;padding:0 7px;border-radius:8px;font-size:10px;font-weight:800}
.conn-badge.green{background:rgba(34,197,94,.18);color:#4ade80}
.conn-badge.gray{background:rgba(148,163,184,.15);color:#94a3b8}
.conn-badge.orange{background:rgba(245,158,11,.18);color:#fbbf24}
.conn-badge.red{background:rgba(239,68,68,.18);color:#f87171}


.bottom-bulk{position:fixed;left:0;right:0;bottom:0;z-index:400;display:none;padding:12px 16px;background:var(--card);border-top:1px solid var(--card-b);backdrop-filter:blur(12px)}
.bottom-bulk.show{display:block}
.bottom-bulk-inner{max-width:960px;margin:0 auto;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:center}
.bottom-bulk select{padding:8px 10px;border-radius:10px;border:1px solid var(--card-b);background:var(--input-bg);color:var(--t1);font-family:inherit;font-size:12px}

table th:first-child, table td:first-child{overflow:visible}
.cfg-chk{accent-color:var(--accent)}
#page-donate .page-title{width:100%}
</style>
</head>

<body>

<div class="card">

<div class="brand">

<div class="logo">P</div>

<div>
<div class="brand-name">
AGN021G
</div>

<div class="version">
13.8.0
</div>
</div>

</div>

<div class="status">
● سیستم آنلاین و فعال است
</div>

<h1>
برای ورود به پنل
<br>
ابتدا وارد شوید
</h1>

<div class="desc">
این صفحه، درگاه عمومی AGN021G است.
برای دسترسی به داشبورد مدیریت از مسیر ورود استفاده کنید.
</div>

<div class="path">
/login
</div>

<div class="actions">

<a
href="/login"
class="btn primary"
>
ورود به پنل
</a>



</div>

<div class="footer">
<span>AGN021G</span>
</div>

</div>


<div id="bottomBulkBar" class="bottom-bulk">
  <div class="bottom-bulk-inner">
    <span id="bulkCount">0 انتخاب</span>
    <select id="bulkGroup"></select>
    <button class="btn btn-sm" onclick="bulkMoveGroup()">انتقال به گروه</button>
    <button class="btn btn-sm btn-d" onclick="bulkDelete()">حذف انتخاب‌شده</button>
    <button class="btn btn-sm" onclick="clearSelection()">لغو</button>
  </div>
</div>
</body>
</html>
"""


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def root(
    request: Request,
):
    # Only reached via /{PANEL_PATH}/ after middleware rewrite
    if await is_valid_session(
        request.cookies.get(
            SESSION_COOKIE
        )
    ):
        return RedirectResponse(f"/{PANEL_PATH}/dashboard")
    return RedirectResponse(f"/{PANEL_PATH}/login")


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "connections": len(connections),
        "uptime": uptime(),
    }



def inject_panel_base(html: str, request: Request | None = None) -> str:
    """Inject secret base path so frontend API/redirects stay under PANEL_PATH."""
    base = "/" + PANEL_PATH
    host = "localhost"
    if request is not None:
        try:
            host = get_host(request)
        except Exception:
            host = request.headers.get("host") or "localhost"
    login_url = f"https://{host}/{PANEL_PATH}/login"
    html = html.replace("__PANEL_PATH_DISPLAY__", "/" + PANEL_PATH)
    html = html.replace("__PANEL_LOGIN_URL__", login_url)
    # Unique marker so we don't skip injection just because JS *reads* PANEL_BASE
    marker = "/*__PANEL_BASE_INJECTED__*/"
    snippet = f"<script>{marker}window.PANEL_BASE={base!r};</script>"
    if marker not in html:
        if "</head>" in html:
            html = html.replace("</head>", snippet + "\n</head>", 1)
        else:
            html = snippet + html
    return html



# ============================================================
# LOGIN
# ============================================================

LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>پنل</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
  font-family:Vazirmatn,sans-serif;color:#f1f5f9;background:#07070f;position:relative;overflow:hidden;
}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 60% 50% at 20% 30%, rgba(59,130,246,.35), transparent 55%),
    radial-gradient(ellipse 50% 40% at 80% 20%, rgba(168,85,247,.28), transparent 50%),
    radial-gradient(ellipse 45% 40% at 60% 80%, rgba(34,211,238,.2), transparent 50%);
  animation:loginBg 14s ease-in-out infinite alternate;
}
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.03) 1px,transparent 1px);
  background-size:48px 48px;mask-image:radial-gradient(ellipse at center,black 20%,transparent 70%);
}
@keyframes loginBg{0%{transform:scale(1) translate(0,0)}100%{transform:scale(1.08) translate(-2%,1%)}}
.card{position:relative;z-index:1}

.card{
  width:100%;max-width:min(400px,100%);padding:28px 24px;border-radius:18px;
  background:#12121a;border:1px solid rgba(255,255,255,.08);
}
h1{font-size:20px;font-weight:800;text-align:center;margin-bottom:22px;letter-spacing:-.02em}
label{display:block;font-size:12px;color:rgba(255,255,255,.5);margin-bottom:6px;font-weight:600}
input{
  width:100%;padding:12px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.1);
  background:rgba(0,0,0,.35);color:#fff;font-family:inherit;font-size:14px;outline:none;margin-bottom:14px;
  direction:ltr;text-align:left;
}
input:focus{border-color:rgba(59,130,246,.55)}
button{
  width:100%;padding:13px;border:none;border-radius:12px;
  background:#2563eb;color:#fff;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer;margin-top:4px;
}
button:hover{background:#1d4ed8}
button:disabled{opacity:.5;cursor:not-allowed}
.err{display:none;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.28);color:#fca5a5;padding:10px 12px;border-radius:10px;font-size:12px;margin-bottom:12px}
.err.show{display:block}
.warn{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.28);border-radius:12px;padding:12px;font-size:12px;line-height:1.85;color:#fbbf24;margin-bottom:16px}
.warn code{background:rgba(0,0,0,.35);padding:2px 6px;border-radius:6px;font-family:ui-monospace,monospace;color:#93c5fd}
.hidden{display:none}

@media(max-width:420px){
  body{padding:12px!important}
  .card{padding:22px 16px!important;border-radius:20px!important}
  h1{font-size:20px!important}
  input,button{min-height:48px!important;font-size:16px!important}
}
@media(min-width:1200px){
  .card{max-width:420px}
}
</style>

</head>
<body>
<div class="card">
  <h1>پنل</h1>

  <div id="setupBox" class="hidden">
    <div class="warn">
      برای نگه‌داشتن داده‌ها روی Railway حتماً Volume با مسیر <code>/data</code> وصل کنید.
    </div>
    <div class="err" id="setupErr"></div>
    <label>رمز عبور پنل</label>
    <input type="password" id="setupPw" placeholder="حداقل ۶ کاراکتر" autocomplete="new-password">
    <label>تکرار رمز عبور</label>
    <input type="password" id="setupPw2" placeholder="تکرار رمز" autocomplete="new-password">
    <button type="button" id="setupBtn" onclick="doSetup()">تنظیم رمز و ورود</button>
  </div>

  <div id="loginBox" class="hidden">
    <div class="err" id="loginErr"></div>
    <form id="loginForm">
      <label>نام کاربری ادمین</label>
      <input type="text" id="loginUser" placeholder="اختیاری" autocomplete="username">
      <label>رمز عبور</label>
      <input type="password" id="loginPw" placeholder="رمز عبور" autocomplete="current-password" required>
      <button type="submit" id="loginBtn">ورود</button>
    </form>
  </div>
</div>
<script>
async function checkSetup(){
  const setup=document.getElementById('setupBox');
  const login=document.getElementById('loginBox');
  try{
    const r=await fetch((window.PANEL_BASE||'')+'/api/setup/status',{cache:'no-store'});
    const d=await r.json();
    if(d && d.needs_setup){
      setup.classList.remove('hidden');
      login.classList.add('hidden');
      document.getElementById('setupPw')?.focus();
      return;
    }
    setup.classList.add('hidden');
    login.classList.remove('hidden');
    document.getElementById('loginPw')?.focus();
  }catch(e){
    // if status unknown, show BOTH so user can set password if first run
    setup.classList.remove('hidden');
    login.classList.remove('hidden');
  }
}
async function doSetup(){
  const pw=document.getElementById('setupPw').value;
  const pw2=document.getElementById('setupPw2').value;
  const err=document.getElementById('setupErr');
  err.classList.remove('show');
  if(pw.length<6){err.textContent='رمز حداقل ۶ کاراکتر';err.classList.add('show');return}
  if(pw!==pw2){err.textContent='تکرار رمز یکسان نیست';err.classList.add('show');return}
  const btn=document.getElementById('setupBtn');btn.disabled=true;
  try{
    const r=await fetch((window.PANEL_BASE||'')+'/api/setup/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw,repeat_password:pw2})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(d.detail||'خطا');
    location.href=(window.PANEL_BASE||'')+'/dashboard';
  }catch(e){
    err.textContent=e.message||'خطا';err.classList.add('show');
    btn.disabled=false;
  }
}
document.getElementById('loginForm').addEventListener('submit',async e=>{
  e.preventDefault();
  const err=document.getElementById('loginErr');
  err.classList.remove('show');
  const btn=document.getElementById('loginBtn');btn.disabled=true;
  try{
    const r=await fetch((window.PANEL_BASE||'')+'/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      password:document.getElementById('loginPw').value,
      username:document.getElementById('loginUser').value
    })});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      throw new Error(d.detail||'رمز اشتباه است');
    }
    location.href=(window.PANEL_BASE||'')+'/dashboard';
  }catch(e){
    err.textContent=e.message;err.classList.add('show');
    btn.disabled=false;
  }
});
checkSetup();
</script>
</body>
</html>
"""




def login_error_html(
    message: str,
):
    safe_message = escape_html(
        message
    )

    html = LOGIN_HTML.replace(
        "</form>",
        (
            f"""
            <div class="error">
                {safe_message}
            </div>
            </form>
            """
        ),
    )
    return inject_panel_base(html)



# ============================================================
# FIRST-RUN SETUP
# ============================================================

def _owner_password_ready() -> bool:
    h = (AUTH.get("password_hash") or "").strip()
    return bool(AUTH.get("password_configured") and h)


@app.get("/api/setup/status")
async def setup_status():
    ready = _owner_password_ready()
    return {
        "password_configured": ready,
        "needs_setup": not ready,
    }


@app.post("/api/setup/password")
async def setup_password(request: Request):
    if _owner_password_ready():
        raise HTTPException(status_code=400, detail="رمز قبلاً تنظیم شده است")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="اطلاعات نامعتبر")
    pw = str(body.get("password") or "")
    rp = str(body.get("repeat_password") or body.get("confirm") or "")
    if len(pw) < 6:
        raise HTTPException(status_code=400, detail="رمز باید حداقل ۶ کاراکتر باشد")
    if pw != rp:
        raise HTTPException(status_code=400, detail="تکرار رمز یکسان نیست")
    AUTH["password_hash"] = hash_password(pw)
    AUTH["password_configured"] = True
    await save_state()
    token = await create_session()
    response = JSONResponse({"ok": True, "message": "رمز تنظیم شد"})
    set_auth_cookie(response, request, token)
    log_activity("auth", "رمز اولیه پنل تنظیم شد", "ok")
    return response


@app.get(
    "/login",
    response_class=HTMLResponse,
)
async def login_page(
    request: Request,
):

    if await is_valid_session(
        request.cookies.get(
            SESSION_COOKIE
        )
    ):
        return RedirectResponse(f"/{PANEL_PATH}/dashboard")

    return HTMLResponse(
        inject_panel_base(LOGIN_HTML, request)
    )


@app.post("/login")
async def login_form(
    request: Request,
):
    if not (AUTH.get("password_configured") and AUTH.get("password_hash")):
        return HTMLResponse(login_error_html("ابتدا از صفحه ورود، رمز اولیه را تنظیم کنید"))


    try:

        content_type = (
            request.headers
            .get(
                "content-type",
                "",
            )
            .lower()
        )

        if "application/json" in content_type:

            body = await request.json()

            password = str(
                body.get(
                    "password",
                    "",
                )
            ).strip()

        else:

            raw = await request.body()

            parsed = parse_qs(
                raw.decode(
                    "utf-8",
                    errors="ignore",
                )
            )

            password = (
                parsed.get(
                    "password",
                    [""],
                )[0]
                .strip()
            )

    except Exception as exc:

        logger.exception(
            "Login parser error: %s",
            exc,
        )

        return HTMLResponse(
            login_error_html(
                "خطا در پردازش اطلاعات ورود."
            ),
            status_code=400,
        )

    ip = client_ip(request)

    blocked, retry_after = login_is_blocked(ip)
    if blocked:
        minutes = max(1, (retry_after + 59) // 60)
        return HTMLResponse(
            login_error_html(
                f"به دلیل تلاش‌های ناموفق متعدد، ورود موقتاً مسدود شده است. حدود {minutes} دقیقه دیگر دوباره تلاش کنید."
            ),
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    if not password:
        register_login_failure(ip)
        return HTMLResponse(
            login_error_html(
                "رمز عبور را وارد کنید."
            ),
            status_code=400,
        )

    if (
        hash_password(password)
        != AUTH["password_hash"]
    ):

        locked, value = register_login_failure(ip)
        if locked:
            return HTMLResponse(
                login_error_html(
                    "تعداد تلاش‌های ناموفق بیش از حد مجاز بود. این IP برای ۱۵ دقیقه مسدود شد."
                ),
                status_code=429,
                headers={"Retry-After": str(LOGIN_LOCKOUT_SECONDS)},
            )

        remaining = value
        log_activity(
            "auth",
            (
                f"تلاش ورود ناموفق از {ip}؛ "
                f"{remaining} تلاش باقی مانده"
            ),
            "err",
        )

        return HTMLResponse(
            login_error_html(
                f"رمز عبور اشتباه است. {remaining} تلاش دیگر باقی مانده است."
            ),
            status_code=401,
        )

    clear_login_failures(ip)

    token = await create_session()

    response = RedirectResponse(
        f"/{PANEL_PATH}/dashboard?login=1",
        status_code=303,
    )

    set_auth_cookie(
        response,
        request,
        token,
    )

    log_activity(
        "auth",
        (
            f"ورود موفق به پنل "
            f"از {client_ip(request)}"
        ),
        "ok",
    )

    return response


@app.post("/api/login")
async def api_login(request: Request):
    if not (AUTH.get("password_configured") and AUTH.get("password_hash")):
        raise HTTPException(status_code=400, detail="ابتدا رمز پنل را در راه‌اندازی تنظیم کنید")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON نامعتبر است")
    password = str(body.get("password", "")).strip()
    username = str(body.get("username", "")).strip().lower()
    ip = client_ip(request)
    blocked, retry_after = login_is_blocked(ip)
    if blocked:
        raise HTTPException(status_code=429, detail=f"ورود موقتاً مسدود است. حدود {max(1, (retry_after + 59) // 60)} دقیقه دیگر تلاش کنید.", headers={"Retry-After": str(retry_after)})
    if not password:
        register_login_failure(ip)
        raise HTTPException(status_code=400, detail="رمز عبور الزامی است")
    meta = {"role": "owner", "admin_id": None, "username": "owner"}
    ok = False
    if username and username not in ("owner", "admin", "root"):
        aid, admin = find_admin_by_username(username)
        if admin and admin.get("password_hash") == hash_password(password):
            if not admin_is_valid(admin):
                raise HTTPException(status_code=403, detail="حساب مسدود یا منقضی شده است")
            ok = True
            meta = {"role": "admin", "admin_id": aid, "username": username}
    else:
        if hash_password(password) == AUTH["password_hash"]:
            ok = True
    if not ok:
        locked, value = register_login_failure(ip)
        if locked:
            raise HTTPException(status_code=429, detail="تعداد تلاش بیش از حد. ۱۵ دقیقه صبر کنید.", headers={"Retry-After": str(LOGIN_LOCKOUT_SECONDS)})
        raise HTTPException(status_code=401, detail=f"نام کاربری یا رمز اشتباه است. {value} تلاش باقی‌مانده")
    clear_login_failures(ip)
    token = await create_session(meta)
    response = JSONResponse({"ok": True, "role": meta["role"], "username": meta["username"]})
    set_auth_cookie(response, request, token)
    log_activity("auth", f"ورود موفق ({meta['username']}) از {ip}", "ok")
    return response


@app.post("/api/logout")
async def api_logout(
    request: Request,
):

    await destroy_session(
        request.cookies.get(
            SESSION_COOKIE
        )
    )

    response = JSONResponse(
        {
            "ok": True
        }
    )

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
    )

    return response





# ============================================================
# CHANGE PASSWORD
# ============================================================

@app.post("/api/change-password")
async def api_change_password(
    request: Request,
    token=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="اطلاعات نامعتبر است",
        )

    current_password = str(
        body.get(
            "current_password",
            "",
        )
    )

    if (
        hash_password(current_password)
        != AUTH["password_hash"]
    ):
        raise HTTPException(
            status_code=400,
            detail="رمز فعلی اشتباه است",
        )

    new_password = str(
        body.get(
            "new_password",
            "",
        )
    )

    repeat_password = str(
        body.get(
            "repeat_password",
            "",
        )
    )

    if len(new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="رمز جدید باید حداقل ۶ کاراکتر باشد",
        )

    if new_password != repeat_password:
        raise HTTPException(
            status_code=400,
            detail="تکرار رمز عبور یکسان نیست",
        )

    AUTH[
        "password_hash"
    ] = hash_password(
        new_password
    )

    async with SESSIONS_LOCK:

        SESSIONS.clear()

        SESSIONS[token] = (
            time.time()
            + SESSION_TTL
        )

    await save_state()

    log_activity(
        "auth",
        "رمز عبور پنل تغییر کرد",
        "ok",
    )

    return {
        "ok": True
    }


# ============================================================
# CREATE LINK
# ============================================================

@app.post("/api/links")
async def create_link_api(
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()

        if not isinstance(body, dict):
            raise ValueError(
                "body is not object"
            )

    except Exception as exc:

        logger.exception(
            "Create link JSON error: %s",
            exc,
        )

        raise HTTPException(
            status_code=400,
            detail="اطلاعات ارسال‌شده معتبر نیست.",
        )

    limit_value = safe_float(
        body.get(
            "limit_value",
            0,
        )
    )

    limit_unit = str(
        body.get(
            "limit_unit",
            "GB",
        )
        or "GB"
    ).upper()

    limit_bytes = (
        0
        if limit_value <= 0
        else parse_size_to_bytes(
            limit_value,
            limit_unit,
        )
    )

    expires_days = safe_int(
        body.get(
            "expires_days",
            0,
        ),
        minimum=0,
    )

    expires_at = (
        (
            datetime.now()
            + timedelta(
                days=expires_days
            )
        ).isoformat()
        if expires_days > 0
        else None
    )

    port = safe_int(
        body.get(
            "port",
            DEFAULT_PORT,
        ),
        default=DEFAULT_PORT,
        minimum=MIN_PORT,
        maximum=MAX_PORT,
    )

    ip_limit = safe_int(
        body.get(
            "ip_limit",
            0,
        ),
        minimum=0,
    )

    speed_value = safe_float(
        body.get(
            "speed_limit_value",
            0,
        )
    )

    speed_unit = str(
        body.get(
            "speed_limit_unit",
            "MBIT",
        )
        or "MBIT"
    ).upper()

    speed_bytes = (
        0
        if speed_value <= 0
        else parse_speed_to_bytes(
            speed_value,
            speed_unit,
        )
    )

    connection_limit = safe_int(
        body.get(
            "connection_limit",
            0,
        ),
        minimum=0,
    )

    protocol = str(
        body.get(
            "protocol",
            DEFAULT_PROTOCOL,
        )
        or DEFAULT_PROTOCOL
    ).strip()

    if protocol not in PROTOCOLS:
        protocol = DEFAULT_PROTOCOL

    fingerprint = str(
        body.get(
            "fingerprint",
            DEFAULT_FINGERPRINT,
        )
        or DEFAULT_FINGERPRINT
    ).strip().lower()

    if fingerprint not in FINGERPRINTS:
        fingerprint = DEFAULT_FINGERPRINT

    fragment = str(
        body.get(
            "fragment",
            "off",
        )
        or "off"
    ).strip().lower()

    allowed_fragments = {
        "off",
        "safe",
        "balanced",
        "aggressive",
    }

    if fragment not in allowed_fragments:
        fragment = "off"

    raw_clean = body.get("clean_ips") or body.get("clean_ip") or ""
    if isinstance(raw_clean, list):
        clean_ips = [str(x).strip() for x in raw_clean if str(x).strip()]
    else:
        clean_ips = [x.strip() for x in str(raw_clean).replace(",", "\n").splitlines() if x.strip()]
    alarm_enabled = bool(body.get("alarm_enabled", False))
    category_id = str(body.get("category_id") or "0")
    if category_id not in CATEGORIES:
        category_id = "0"
    config_count = safe_int(body.get("config_count", 1), minimum=1, maximum=40)
    cat = CATEGORIES.get(category_id) or {}
    if cat.get("limit_bytes") and limit_bytes <= 0:
        limit_bytes = int(cat["limit_bytes"])
    if cat.get("expires_days") and expires_days <= 0:
        expires_days = int(cat["expires_days"])
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days > 0 else None
    if cat.get("connection_limit") and connection_limit <= 0:
        connection_limit = int(cat["connection_limit"])
    if cat.get("speed_limit_bytes") and speed_bytes <= 0:
        speed_bytes = int(cat["speed_limit_bytes"])
    if cat.get("ip_limit") and ip_limit <= 0:
        ip_limit = int(cat["ip_limit"])
    if cat.get("clean_ips") and not clean_ips:
        clean_ips = list(cat["clean_ips"])
    if cat.get("single_user"):
        if ip_limit == 0: ip_limit = 1
        if connection_limit == 0: connection_limit = 1
    label_val = body.get("label", "")
    if cat.get("random_name") or not str(label_val).strip():
        label_val = random_config_name()
    else:
        label_val = sanitize_config_name(str(label_val))

    uid, link = await make_link(
        label=label_val,
        limit_bytes=limit_bytes,
        expires_at=expires_at,
        note=body.get(
            "note",
            "",
        ),
        sub_id=body.get(
            "sub_id"
        ),
        protocol=protocol,
        fingerprint=fingerprint,
        alpn=body.get(
            "alpn",
            DEFAULT_ALPN_BY_PROTOCOL.get(
                protocol,
                "http/1.1",
            ),
        ),
        port=port,
        ip_limit=ip_limit,
        speed_limit_bytes=speed_bytes,
        connection_limit=connection_limit,
        fragment=fragment,
        clean_ips=clean_ips,
        alarm_enabled=alarm_enabled,
        category_id=category_id,
        config_count=config_count,
    )

    host = get_host(request)

    result = {
        **get_link_info(
            link,
            uid,
            host,
        ),
        "ok": True,
    }

    return result


# ============================================================
# AUTO CREATE
# ============================================================

@app.post("/api/links/auto")
async def create_auto_link(
    request: Request,
    _=Depends(require_auth),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict): body = {}
    host = get_host(request)
    protocol = normalize_protocol(body.get("protocol", DEFAULT_PROTOCOL))
    profile = str(body.get("profile", "balanced")).strip().lower()
    profiles = {
        "normal": {"ip": 0, "conn": 0, "speed": 0, "fp": "chrome", "fragment": "off"},
        "balanced": {"ip": 2, "conn": 6, "speed": 0, "fp": "chrome", "fragment": "safe"},
        "gaming": {"ip": 1, "conn": 3, "speed": 0, "fp": "chrome", "fragment": "safe"},
        "speed": {"ip": 0, "conn": 0, "speed": 0, "fp": "chrome", "fragment": "off"},
        "stable": {"ip": 2, "conn": 4, "speed": 0, "fp": "firefox", "fragment": "tlshello"},
        "maximum": {"ip": 0, "conn": 0, "speed": 0, "fp": "randomized", "fragment": "safe"},
    }
    cfg = profiles.get(profile, profiles["balanced"])
    config_count = safe_int(body.get("config_count", 1), minimum=1, maximum=40)
    uid, link = await make_link(
        label=auto_config_name(), limit_bytes=0, expires_at=None,
        ip_limit=cfg["ip"], speed_limit_bytes=cfg["speed"], connection_limit=cfg["conn"],
        note=f"Auto generated | profile={profile}",
        protocol=protocol, fingerprint=cfg["fp"],
        alpn=DEFAULT_ALPN_BY_PROTOCOL.get(protocol, ""), port=443, fragment=cfg["fragment"],
        config_count=config_count,
    )
    link["security_profile"] = profile
    result = {**get_link_info(link, uid, host), "ok": True, "profile": profile}
    log_activity("link", f"کانفیگ خودکار «{link['label']}» با {PROTOCOL_LABELS.get(protocol, protocol)} ساخته شد", "ok")
    return result


# ============================================================
# LIST LINKS
# ============================================================


@app.post("/api/links/multi-auto")
async def create_multi_auto_link(request: Request, _=Depends(require_auth)):
    """Create a named subscription containing multiple protocols (counts per protocol)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    host = get_host(request)
    sub_name = str(body.get("sub_name") or body.get("name") or "").strip()[:60]
    if not sub_name:
        sub_name = "ساب " + secrets.token_hex(3)

    # protocols: {"vless-ws": 2, "hysteria2": 1, ...} or list of {id, count}
    raw = body.get("protocols") or {}
    counts: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            pid = normalize_protocol(k)
            n = safe_int(v, minimum=0, maximum=20)
            if n > 0 and pid in PROTOCOLS:
                counts[pid] = n
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            pid = normalize_protocol(item.get("id") or item.get("protocol"))
            n = safe_int(item.get("count", 1), minimum=0, maximum=20)
            if n > 0 and pid in PROTOCOLS:
                counts[pid] = counts.get(pid, 0) + n

    if not counts:
        # fallback: one vless
        counts[DEFAULT_PROTOCOL] = 1

    limit_value = safe_float(body.get("limit_value", 0))
    limit_unit = str(body.get("limit_unit", "GB") or "GB").upper()
    limit_bytes = 0 if limit_value <= 0 else parse_size_to_bytes(limit_value, limit_unit)
    expires_days = safe_int(body.get("expires_days", 0), minimum=0)
    expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat() if expires_days > 0 else None
    ip_limit = safe_int(body.get("ip_limit", 0), minimum=0)
    speed_value = safe_float(body.get("speed_limit_value", 0))
    speed_bytes = 0 if speed_value <= 0 else parse_speed_to_bytes(speed_value, "MBIT")

    profile = str(body.get("profile", "balanced")).strip().lower()
    profiles = {
        "normal": {"ip": 0, "conn": 0, "fp": "chrome", "fragment": "off"},
        "balanced": {"ip": 2, "conn": 6, "fp": "chrome", "fragment": "safe"},
        "gaming": {"ip": 1, "conn": 3, "fp": "chrome", "fragment": "safe"},
        "speed": {"ip": 0, "conn": 0, "fp": "chrome", "fragment": "off"},
        "stable": {"ip": 2, "conn": 4, "fp": "firefox", "fragment": "tlshello"},
        "maximum": {"ip": 0, "conn": 0, "fp": "randomized", "fragment": "safe"},
    }
    cfg = profiles.get(profile, profiles["balanced"])
    if ip_limit <= 0:
        ip_limit = cfg["ip"]

    sub_id, sub = await create_sub_group(name=sub_name, desc="ساخت خودکار چندپروتکلی")
    if limit_bytes > 0:
        async with SUBS_LOCK:
            if sub_id in SUBS:
                SUBS[sub_id]["limit_bytes"] = limit_bytes
                sub["limit_bytes"] = limit_bytes

    # dual endpoints: default (Railway HTTPS) + custom TCP Proxy if configured
    default_host = host
    endpoints = [("main", default_host, DEFAULT_PORT, "tls")]
    pub_host = (NETWORK_CFG.get("public_host") or "").strip()
    pub_port = int(NETWORK_CFG.get("public_port") or 0)
    pub_sec = (NETWORK_CFG.get("public_security") or "tls").strip().lower()
    if pub_sec not in ("tls", "none"):
        pub_sec = "tls"
    if pub_host or (pub_port and pub_port != DEFAULT_PORT):
        eh = pub_host or default_host
        ep = pub_port if pub_port > 0 else DEFAULT_PORT
        # only add second if different from main
        if eh != default_host or ep != DEFAULT_PORT or pub_sec != "tls":
            endpoints.append(("proxy", eh, ep, pub_sec))

    created = []
    total = 0
    for protocol, n in counts.items():
        for i in range(n):
            for ep_tag, ep_host, ep_port, ep_sec in endpoints:
                base_lab = PROTOCOL_LABELS.get(protocol, protocol).split()[0]
                if len(endpoints) > 1:
                    tag = "Main" if ep_tag == "main" else "TCP"
                    label = f"{sub_name}-{base_lab}-{tag}"
                    if n > 1:
                        label = f"{sub_name}-{base_lab}-{tag}-{i+1}"
                else:
                    label = f"{sub_name}-{base_lab}-{i+1}" if n > 1 else f"{sub_name}-{base_lab}"
                uid, link = await make_link(
                    label=sanitize_config_name(label)[:40],
                    limit_bytes=0,
                    expires_at=expires_at,
                    ip_limit=ip_limit,
                    speed_limit_bytes=speed_bytes,
                    connection_limit=cfg["conn"],
                    note=f"Multi-auto | {sub_name} | {protocol} | {ep_tag}",
                    protocol=protocol,
                    fingerprint=cfg["fp"],
                    alpn=DEFAULT_ALPN_BY_PROTOCOL.get(protocol, ""),
                    port=int(ep_port),
                    fragment=cfg["fragment"],
                    sub_id=sub_id,
                    config_count=1,
                )
                link["security_profile"] = profile
                link["endpoint_host"] = ep_host
                link["security"] = ep_sec
                link["port"] = int(ep_port)
                created.append(get_link_info(link, uid, ep_host))
                total += 1

    await save_state()
    log_activity("sub", f"ساب «{sub_name}» با {total} کانفیگ چندپروتکلی ساخته شد", "ok")

    sub_url = f"https://{host}/sub-group/{sub['uuid_key']}"
    page_url = sub_url
    return {
        "ok": True,
        "sub_id": sub_id,
        "sub_name": sub_name,
        "uuid_key": sub["uuid_key"],
        "page": page_url,
        "sub": sub_url,
        "count": total,
        "protocols": counts,
        "links": created,
    }



@app.get("/api/protocols")
async def api_protocols(request: Request):
    require_auth(request)
    return {"protocols": [{"id": p, "label": PROTOCOL_LABELS.get(p, p)} for p in PROTOCOLS], "default": DEFAULT_PROTOCOL}


@app.get("/api/links")
async def list_links(
    request: Request,
    _=Depends(require_auth),
):

    host = get_host(request)

    async with LINKS_LOCK:
        snapshot = dict(LINKS)

    result = []

    for uid, link in snapshot.items():

        info = get_link_info(
            link,
            uid,
            host,
        )

        result.append(
            {
                **info,

                "created_at":
                    link.get(
                        "created_at"
                    ),

                "expired":
                    is_link_expired(
                        link
                    ),

                "sub_url":
                    f"https://{host}/sub/{uid}",

                "info_url":
                    f"https://{host}/info/{uid}",

                "connected_ips":
                    len(
                        unique_ips_for_uuid(
                            uid
                        )
                    ),
            }
        )

    result = sorted(
        result,
        key=lambda item: (
            -int(item.get("sort_order") or 0),
            str(item.get("created_at") or ""),
        ),
    )

    return {
        "links": result
    }


# ============================================================
# LINK INFO API
# ============================================================

@app.get("/api/links/{uid}/info")
async def link_info_api(
    uid: str,
    request: Request,
    _=Depends(require_auth),
):

    async with LINKS_LOCK:

        link = LINKS.get(uid)

        if not link:
            raise HTTPException(
                status_code=404,
                detail="link not found",
            )

        snapshot = dict(link)

    host = get_host(request)

    return {
        "ok": True,
        **get_link_info(
            snapshot,
            uid,
            host,
        ),
    }


# ============================================================
# UPDATE LINK
# ============================================================



@app.post("/api/links/reorder")
async def reorder_links(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="JSON نامعتبر")
    order = body.get("order") or body.get("ids") or []
    if not isinstance(order, list):
        raise HTTPException(400, detail="order باید آرایه باشد")
    # first item = highest priority
    n = len(order)
    async with LINKS_LOCK:
        for i, uid in enumerate(order):
            uid = str(uid)
            if uid in LINKS:
                LINKS[uid]["sort_order"] = n - i
    await save_state()
    return {"ok": True, "count": n}


@app.post("/api/links/bulk-delete")
async def bulk_delete_links(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="JSON نامعتبر")
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, detail="ids خالی است")
    deleted = []
    for uid in ids:
        uid = str(uid)
        if uid in LINKS:
            await remove_link(uid)
            deleted.append(uid)
    log_activity("link", f"حذف گروهی {len(deleted)} کانفیگ", "warn")
    return {"ok": True, "deleted": len(deleted)}


@app.post("/api/links/bulk-category")
async def bulk_category(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="JSON نامعتبر")
    ids = body.get("ids") or []
    cid = str(body.get("category_id") or "0")
    if cid not in CATEGORIES:
        cid = "0"
    n = 0
    async with LINKS_LOCK:
        for uid in ids:
            uid = str(uid)
            if uid in LINKS:
                LINKS[uid]["category_id"] = cid
                n += 1
    await save_state()
    return {"ok": True, "updated": n}


@app.patch("/api/links/{uid}")
async def update_link(
    uid: str,
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="اطلاعات نامعتبر است",
        )

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail="اطلاعات نامعتبر است",
        )

    async with LINKS_LOCK:

        if uid not in LINKS:
            raise HTTPException(
                status_code=404,
                detail="link not found",
            )

        link = LINKS[uid]

        old_sub = link.get(
            "sub_id"
        )

        label = link.get(
            "label",
            uid,
        )

        if "active" in body:
            link["active"] = bool(
                body["active"]
            )

        if "category_id" in body:
            cid = str(body.get("category_id") or "0")
            if cid not in CATEGORIES:
                cid = "0"
            link["category_id"] = cid

        if "sort_order" in body:
            try:
                link["sort_order"] = int(body.get("sort_order") or 0)
            except Exception:
                pass

        if "label" in body:

            value = str(
                body["label"]
            ).strip()

            if value:
                link["label"] = value[:60]

        if "note" in body:

            link["note"] = str(
                body.get(
                    "note",
                    "",
                )
            )[:500]

        if "reset_usage" in body:

            if body.get(
                "reset_usage"
            ):
                link[
                    "used_bytes"
                ] = 0


        if "limit_value" in body:

            value = safe_float(
                body.get(
                    "limit_value",
                    0,
                )
            )

            unit = str(
                body.get(
                    "limit_unit",
                    "GB",
                )
                or "GB"
            )

            link[
                "limit_bytes"
            ] = (
                0
                if value <= 0
                else parse_size_to_bytes(
                    value,
                    unit,
                )
            )

        if "expires_days" in body:

            days = safe_int(
                body.get(
                    "expires_days",
                    0,
                ),
                minimum=0,
            )

            link[
                "expires_at"
            ] = (
                (
                    datetime.now()
                    + timedelta(
                        days=days
                    )
                ).isoformat()
                if days > 0
                else None
            )

        if "fingerprint" in body:

            fingerprint = str(
                body.get(
                    "fingerprint",
                    DEFAULT_FINGERPRINT,
                )
            ).strip().lower()

            link[
                "fingerprint"
            ] = (
                fingerprint
                if fingerprint in FINGERPRINTS
                else DEFAULT_FINGERPRINT
            )

        if "alpn" in body:

            link["alpn"] = str(
                body.get(
                    "alpn",
                    "",
                )
            )[:100]

        if "port" in body:

            p = safe_int(
                body.get(
                    "port",
                    DEFAULT_PORT,
                ),
                default=DEFAULT_PORT,
                minimum=MIN_PORT,
                maximum=MAX_PORT,
            )

            link["port"] = p

        if "ip_limit" in body:

            link["ip_limit"] = safe_int(
                body.get(
                    "ip_limit",
                    0,
                ),
                minimum=0,
            )

        if "connection_limit" in body:

            link[
                "connection_limit"
            ] = safe_int(
                body.get(
                    "connection_limit",
                    0,
                ),
                minimum=0,
            )

        if "speed_limit_value" in body:

            speed_value = safe_float(
                body.get(
                    "speed_limit_value",
                    0,
                )
            )

            speed_unit = str(
                body.get(
                    "speed_limit_unit",
                    "MBIT",
                )
                or "MBIT"
            )

            link[
                "speed_limit_bytes"
            ] = (
                0
                if speed_value <= 0
                else parse_speed_to_bytes(
                    speed_value,
                    speed_unit,
                )
            )

        if "protocol" in body:

            protocol = str(
                body.get(
                    "protocol",
                    DEFAULT_PROTOCOL,
                )
            ).strip()

            link["protocol"] = (
                protocol
                if protocol in PROTOCOLS
                else DEFAULT_PROTOCOL
            )

        if "fragment" in body:

            fragment = str(
                body.get(
                    "fragment",
                    "off",
                )
                or "off"
            ).strip().lower()

            if fragment not in {
                "off",
                "safe",
                "balanced",
                "aggressive",
            }:
                fragment = "off"

            link["fragment"] = fragment

        if "sub_id" in body:

            link[
                "sub_id"
            ] = (
                body.get(
                    "sub_id"
                )
                or None
            )

        new_sub = body.get(
            "sub_id",
            "UNCHANGED",
        )

    if new_sub != "UNCHANGED":

        async with SUBS_LOCK:

            if (
                old_sub
                and old_sub in SUBS
            ):

                ids = SUBS[
                    old_sub
                ].get(
                    "link_ids",
                    [],
                )

                if uid in ids:
                    ids.remove(uid)

            if (
                new_sub
                and new_sub in SUBS
            ):

                ids = SUBS[
                    new_sub
                ].setdefault(
                    "link_ids",
                    [],
                )

                if uid not in ids:
                    ids.append(uid)

    await save_state()

    log_activity(
        "link",
        (
            f"کانفیگ "
            f"«{label}» "
            f"ویرایش شد"
        ),
        "info",
    )

    return {
        "ok": True
    }


# ============================================================
# RESET USAGE
# ============================================================

@app.post(
    "/api/links/{uid}/reset-usage"
)
async def reset_link_usage(
    uid: str,
    _=Depends(require_auth),
):

    async with LINKS_LOCK:

        link = LINKS.get(uid)

        if not link:
            raise HTTPException(
                status_code=404,
                detail="link not found",
            )

        link["used_bytes"] = 0

        label = link.get(
            "label",
            uid,
        )

    await save_state()

    log_activity(
        "link",
        (
            f"مصرف کانفیگ "
            f"«{label}» ریست شد"
        ),
        "info",
    )

    return {
        "ok": True,
        "uuid": uid,
        "used_bytes": 0,
    }


# ============================================================
# LINK ACTION
# ============================================================

@app.post(
    "/api/links/{uid}/action"
)
async def link_action(
    uid: str,
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON نامعتبر است",
        )

    action = str(
        body.get(
            "action",
            "",
        )
    ).strip().lower()

    if action == "reset":

        await reset_link_usage(
            uid,
            _
        )

        return {
            "ok": True,
            "action": "reset",
        }

    if action == "enable":

        result = await set_link_active(
            uid,
            True,
        )

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="link not found",
            )

        return {
            "ok": True,
            "action": "enable",
        }

    if action == "disable":

        result = await set_link_active(
            uid,
            False,
        )

        if result is None:
            raise HTTPException(
                status_code=404,
                detail="link not found",
            )

        return {
            "ok": True,
            "action": "disable",
        }

    raise HTTPException(
        status_code=400,
        detail="unknown action",
    )


# ============================================================
# DELETE LINK
# ============================================================

@app.delete("/api/links/{uid}")
async def delete_link(
    uid: str,
    _=Depends(require_auth),
):

    label = await remove_link(uid)

    if label is None:
        raise HTTPException(
            status_code=404,
            detail="link not found",
        )

    return {
        "ok": True,
        "deleted": uid,
    }




def subscription_metadata_headers(used_bytes: int, limit_bytes: int, expires_at, host: str, info_url: str, title: str):
    """Standard subscription headers understood by v2rayNG/v2rayN/Hiddify and similar clients."""
    used_bytes = max(0, int(used_bytes or 0))
    limit_bytes = max(0, int(limit_bytes or 0))

    expire_unix = 0
    if expires_at:
        try:
            dt = datetime.fromisoformat(str(expires_at))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IRAN_TZ) if IRAN_TZ else dt
            expire_unix = max(0, int(dt.timestamp()))
        except Exception:
            expire_unix = 0

    userinfo = f"upload=0; download={used_bytes}; total={limit_bytes}; expire={expire_unix}"

    return {
        "profile-title": quote(title, safe=""),
        "profile-web-page-url": info_url,
        "support-url": SUPPORT_URL,
        "profile-update-interval": "12",
        "subscription-userinfo": userinfo,
        "content-disposition": 'inline; filename="subscription.txt"',
    }

# ============================================================
# SINGLE SUB
# ============================================================


def client_wants_html(request: Request) -> bool:
    """Browser → HTML usage page; VPN clients → subscription body. Same URL."""
    q = request.query_params
    if str(q.get("raw") or "").lower() in ("1", "true", "yes"):
        return False
    if str(q.get("html") or "").lower() in ("1", "true", "yes"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    ua = (request.headers.get("user-agent") or "").lower()
    client_markers = (
        "v2ray", "clash", "sing-box", "singbox", "shadowrocket", "quantumult",
        "surge", "stash", "hiddify", "nekobox", "nekoray", "streisand", "foxray",
        "v2box", "happ", "okhttp", "dart/", "go-http", "proxy", "sfa/", "sfm/",
        "loon", "pharos", "surfboard", "kitsunebi", "shadowsocks",
    )
    if any(m in ua for m in client_markers):
        return False
    if "text/html" in accept:
        return True
    browser_markers = ("mozilla/", "chrome/", "safari/", "firefox/", "edg/", "opr/", "opera")
    if any(m in ua for m in browser_markers):
        return True
    return False


@app.get("/sub/{uuid}")
async def subscription_single(
    uuid: str,
    request: Request,
):
    # One link: browser → info/usage page; client → sub body
    if client_wants_html(request):
        return RedirectResponse(f"/info/{uuid}", status_code=302)

    async with LINKS_LOCK:
        link = LINKS.get(uuid)

    if not is_link_allowed(link):
        raise HTTPException(
            status_code=404,
            detail="not found or inactive",
        )

    host = get_host(request)
    clean_ips = link.get("clean_ips") or []
    used = int(link.get("used_bytes", 0) or 0)
    limit = int(link.get("limit_bytes", 0) or 0)
    remaining = max(0, limit - used) if limit > 0 else 0
    volume_text = f"{fmt_bytes(used)}/{fmt_bytes(limit)} (باقی {fmt_bytes(remaining)})" if limit > 0 else f"{fmt_bytes(used)}/∞"
    expires_at = link.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at))
            now_dt = datetime.now(exp_dt.tzinfo) if getattr(exp_dt, "tzinfo", None) else datetime.now()
            secs = int((exp_dt - now_dt).total_seconds())
            if secs <= 0:
                time_text = "منقضی"
            else:
                days, rem = divmod(secs, 86400)
                hours, rem = divmod(rem, 3600)
                mins = rem // 60
                time_text = f"{days}د {hours}س" if days else (f"{hours}س {mins}د" if hours else f"{mins}د")
        except Exception:
            time_text = str(expires_at)[:16]
    else:
        time_text = "∞"
    label = str(link.get("label") or "Config")
    stats_remark = f"{label} | {volume_text} | {time_text}"
    stats_line = generate_vless_link(uuid, "0.0.0.0", remark=stats_remark, protocol=link.get("protocol", DEFAULT_PROTOCOL), fingerprint=link.get("fingerprint", DEFAULT_FINGERPRINT), alpn=link.get("alpn"), port=link.get("port", DEFAULT_PORT))
    lines = [stats_line]
    used_names = set()
    cfg_count = max(1, min(40, int(link.get("config_count") or 1)))
    if clean_ips:
        hosts = list(clean_ips)
        while len(hosts) < cfg_count:
            hosts.extend(clean_ips)
        hosts = hosts[:cfg_count]
        for cip in hosts:
            name = random_config_name(used_names)
            used_names.add(name)
            lines.append(generate_vless_link(uuid, cip, remark=name, protocol=link.get("protocol", DEFAULT_PROTOCOL), fingerprint=link.get("fingerprint", DEFAULT_FINGERPRINT), alpn=link.get("alpn"), port=link.get("port", DEFAULT_PORT)))
    else:
        for i in range(cfg_count):
            name = random_config_name(used_names)
            used_names.add(name)
            lines.append(generate_vless_link(uuid, (link.get("endpoint_host") or host), remark=name, protocol=link.get("protocol", DEFAULT_PROTOCOL), fingerprint=link.get("fingerprint", DEFAULT_FINGERPRINT), alpn=link.get("alpn"), port=link.get("port", DEFAULT_PORT), security=link.get("security") or None))
    content = base64.b64encode("\n".join(lines).encode()).decode()
    profile_title = f"0.0.0.0 | {stats_remark}"
    headers = subscription_metadata_headers(
        used,
        limit,
        link.get("expires_at"),
        host,
        f"https://{host}/info/{uuid}",
        profile_title,
    )

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )

# ============================================================
# SUB ALL
# ============================================================

@app.get("/sub-all")
async def subscription_all(
    request: Request,
    _=Depends(require_auth),
):

    host = get_host(request)

    async with LINKS_LOCK:

        lines = [
            vless_link_for_link(
                link,
                uid,
                host,
            )

            for uid, link
            in LINKS.items()

            if is_link_allowed(link)
        ]

    content = (
        base64
        .b64encode(
            "\n".join(
                lines
            ).encode()
        )
        .decode()
    )

    return Response(
        content=content,
        media_type="text/plain",
    )


# ============================================================
# INFO PAGE
# ============================================================

@app.get(
    "/info/{uid}",
    response_class=HTMLResponse,
)
async def info_page(
    uid: str,
    request: Request,
):
    async with LINKS_LOCK:
        link = LINKS.get(uid)
        if not link:
            return HTMLResponse("<html lang=\"fa\" dir=\"rtl\"><body style=\"margin:0;background:#07070a;color:#fff;font-family:sans-serif;padding:40px\"><h2>کانفیگ پیدا نشد</h2></body></html>", status_code=404)
        snapshot = dict(link)

    host = get_host(request)
    vless_url = vless_link_for_link(snapshot, uid, host)
    sub_url = f"https://{host}/sub/{uid}"
    used = int(snapshot.get("used_bytes", 0) or 0)
    limit = int(snapshot.get("limit_bytes", 0) or 0)
    if limit > 0:
        usage_percent = max(0, min(100, round((used / limit) * 100, 1)))
        usage_value = f"{fmt_bytes(used)} / {fmt_bytes(limit)}"
        remaining_value = fmt_bytes(max(0, limit - used))
    else:
        usage_percent = 0
        usage_value = f"{fmt_bytes(used)} / نامحدود"
        remaining_value = "نامحدود"

    expires_at = snapshot.get("expires_at")
    if expires_at:
        try:
            expiry_dt = datetime.fromisoformat(str(expires_at))
            now_dt = datetime.now(expiry_dt.tzinfo) if expiry_dt.tzinfo else datetime.now()
            seconds = int((expiry_dt - now_dt).total_seconds())
            if seconds <= 0:
                expiry_remaining = "منقضی شده"
            else:
                days, rem = divmod(seconds, 86400)
                hours, rem = divmod(rem, 3600)
                minutes, _ = divmod(rem, 60)
                expiry_remaining = f"{days} روز و {hours} ساعت" if days else (f"{hours} ساعت و {minutes} دقیقه" if hours else f"{minutes} دقیقه")
        except Exception:
            expiry_remaining = "نامشخص"
        expiry_display = str(expires_at)
    else:
        expiry_remaining = "نامحدود"
        expiry_display = "نامحدود"

    status_text = "فعال" if is_link_allowed(snapshot) else "غیرفعال"
    status_class = "good" if status_text == "فعال" else "bad"
    ip_limit = "نامحدود" if not snapshot.get("ip_limit", 0) else str(snapshot.get("ip_limit"))
    connection_limit = "نامحدود" if not snapshot.get("connection_limit", 0) else str(snapshot.get("connection_limit"))
    speed_limit = "نامحدود" if not snapshot.get("speed_limit_bytes", 0) else fmt_bytes(snapshot.get("speed_limit_bytes", 0)) + "/s"

    usage_history = snapshot.get("usage_history", [])
    svg_points = "0,50 300,50"
    if usage_history and len(usage_history) > 1:
        max_hist = max(usage_history) if max(usage_history) > 0 else 1
        pts = []
        step = 300 / (len(usage_history) - 1)
        for i, val in enumerate(usage_history):
            x = i * step
            y = 60 - min(60, max(4, (val / max_hist) * 52))
            pts.append(f"{x:.1f},{y:.1f}")
        svg_points = " ".join(pts)
    elif usage_history and len(usage_history) == 1:
        svg_points = f"0,50 300,{60 - min(60, max(4, (usage_history[0] / (limit if limit > 0 else max(used, 1))) * 52)):.1f}"

    status_badge_html = 'text-emerald-300 border border-emerald-400/25 bg-emerald-400/10' if status_class == 'good' else 'text-rose-300 border border-rose-400/25 bg-rose-400/10'
    label_escaped = escape_html(snapshot.get("label", "Panel"))
    uid_escaped = escape_html(uid)
    app_version_str = escape_html(str(APP_VERSION))
    used_bytes_str = escape_html(fmt_bytes(used))
    limit_bytes_str = escape_html(fmt_bytes(limit)) if limit > 0 else '∞'
    remaining_value_escaped = escape_html(remaining_value)
    expiry_remaining_escaped = escape_html(expiry_remaining)
    expiry_display_escaped = escape_html(expiry_display)
    ip_limit_escaped = escape_html(ip_limit)
    connection_limit_escaped = escape_html(connection_limit)
    speed_limit_escaped = escape_html(speed_limit)
    protocol_escaped = escape_html(snapshot.get("protocol", "vless-ws"))
    fingerprint_escaped = escape_html(snapshot.get("fingerprint", "chrome"))
    vless_url_escaped = escape_html(vless_url)
    sub_url_escaped = escape_html(sub_url)
    dash_calc_offset = f"{339.29 - (339.29 * min(usage_percent, 100) / 100):.1f}"

    info_html = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{label_escaped} | INFO</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<script>
  tailwind.config = {{
    theme: {{
      extend: {{
        fontFamily: {{ vazir: ['Vazirmatn','system-ui','sans-serif'] }}
      }}
    }}
  }}
</script>
<style>
  :root {{
    --bg-main: #05060a;
    --bg-card: rgba(255, 255, 255, 0.04);
    --bg-card-hover: rgba(255, 255, 255, 0.07);
    --border-color: rgba(255, 255, 255, 0.1);
    --text-main: #f1f5f9;
    --text-muted: rgba(255, 255, 255, 0.4);
    --bg-sub-card: rgba(0, 0, 0, 0.2);
    --grad-1: rgba(96,165,250,.16);
    --grad-2: rgba(96,165,250,.13);
    --grad-3: rgba(52,211,153,.08);
  }}

  body.theme-lighter {{
    --bg-main: #131722;
    --bg-card: rgba(255, 255, 255, 0.075);
    --bg-card-hover: rgba(255, 255, 255, 0.115);
    --border-color: rgba(255, 255, 255, 0.16);
    --text-main: #ffffff;
    --text-muted: rgba(255, 255, 255, 0.6);
    --bg-sub-card: rgba(0, 0, 0, 0.35);
    --grad-1: rgba(96,165,250,.24);
    --grad-2: rgba(96,165,250,.20);
    --grad-3: rgba(52,211,153,.13);
  }}

  html,body{{background:var(--bg-main); transition: background 0.3s ease, color 0.3s ease;}}
  body{{
    background:
      radial-gradient(ellipse 80% 50% at 10% -10%, var(--grad-1), transparent 50%),
      radial-gradient(ellipse 60% 40% at 95% 15%, var(--grad-2), transparent 45%),
      radial-gradient(ellipse 55% 35% at 60% 100%, var(--grad-3), transparent 40%),
      var(--bg-main);
  }}
  .status-dot{{box-shadow:0 0 10px currentColor}}
  ::-webkit-scrollbar{{width:8px;height:8px}}
  ::-webkit-scrollbar-thumb{{background:rgba(255,255,255,.12);border-radius:99px}}
  * {{ box-shadow: none !important; }}
  .copy-btn svg{{transition:none}}
  
  .dynamic-card {{
    background-color: var(--bg-card);
    border-color: var(--border-color);
    transition: background-color 0.3s ease, border-color 0.3s ease;
  }}
  .dynamic-card:hover {{
    background-color: var(--bg-card-hover);
  }}
  .sub-box {{
    background-color: var(--bg-sub-card);
  }}
</style>
</head>
<body class="font-vazir text-slate-100 antialiased min-h-screen py-8 px-3 sm:px-4 md:py-14">

<div class="w-full max-w-4xl mx-auto space-y-5 sm:space-y-6 md:space-y-8">

  <!-- Top Bar Theme Toggle Button -->
  <div class="flex justify-end">
    <button type="button" onclick="toggleTheme()" class="inline-flex items-center gap-1.5 px-4 py-2 rounded-full text-xs font-extrabold text-amber-300 border border-amber-400/30 bg-amber-400/10 hover:bg-amber-400/20 transition-colors shadow-lg">
      <svg id="themeIcon" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
      تغییر تم
    </button>
  </div>

  <!-- Hero -->
  <section class="rounded-[26px] sm:rounded-[28px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-8">
    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-5">
      <div class="flex items-center gap-4">
        <div class="w-13 h-13 sm:w-14 sm:h-14 shrink-0 rounded-2xl grid place-items-center bg-gradient-to-br from-blue-400/20 to-purple-400/10 border border-blue-400/25 text-blue-300">
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l8 4v6c0 5.2-3.4 9-8 10-4.6-1-8-4.8-8-10V6l8-4z"/><path d="M9.5 12l1.8 1.8L15 10"/></svg>
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl md:text-2xl font-black tracking-tight truncate">{label_escaped}</h1>
          <p class="mt-1.5 text-[10.5px] sm:text-[11px] text-white/40 break-all">UUID: {uid_escaped} &nbsp;·&nbsp; AGN021G {app_version_str}</p>
        </div>
      </div>
      <div class="flex items-center gap-2.5 self-start md:self-auto flex-wrap">
        <button type="button" onclick="openQrModal()" class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-full text-xs font-extrabold text-purple-300 border border-purple-400/30 bg-purple-400/10 hover:bg-purple-400/20 transition-colors">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
          QR Code
        </button>
        <div class="inline-flex items-center gap-2 px-4 py-2 rounded-full text-xs font-extrabold {status_badge_html}">
          <span class="status-dot w-2 h-2 rounded-full bg-current"></span>
          {status_text}
        </div>
      </div>
    </div>
  </section>

  <!-- Usage overview -->
  <section class="grid grid-cols-1 lg:grid-cols-[1.6fr_1fr] gap-5 sm:gap-6">

    <div class="rounded-[22px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-7">
      <div class="flex items-center gap-2.5">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="text-white/35"><path d="M3 3v18h18"/><path d="M7 15l4-6 3 3 4-7"/></svg>
        <div>
          <p class="text-[10px] font-extrabold tracking-widest uppercase text-white/30">Traffic Overview</p>
          <p class="mt-0.5 text-sm font-black">مصرف سرویس</p>
        </div>
      </div>

      <div class="mt-6 flex flex-col sm:flex-row items-center sm:items-start gap-6">
        <div class="relative shrink-0 w-[128px] h-[128px]">
          <svg width="128" height="128" viewBox="0 0 132 132" class="-rotate-90">
            <circle cx="66" cy="66" r="54" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="10"/>
            <circle cx="66" cy="66" r="54" fill="none" stroke="url(#usageRingGradient)" stroke-width="10" stroke-linecap="round"
              stroke-dasharray="339.29" stroke-dashoffset="{dash_calc_offset}"/>
            <defs>
              <linearGradient id="usageRingGradient" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stop-color="#34d399"/>
                <stop offset="100%" stop-color="#f59e0b"/>
              </linearGradient>
            </defs>
          </svg>
          <div class="absolute inset-0 grid place-items-center">
            <div class="text-center">
              <p class="text-xl font-black leading-none">{usage_percent}%</p>
              <p class="mt-1.5 text-[10px] text-white/40">مصرف‌شده</p>
            </div>
          </div>
        </div>

        <div class="flex-1 w-full min-w-0">
          <div class="text-xl sm:text-2xl font-black tracking-tight">
            {used_bytes_str}
            <span class="text-sm font-semibold text-white/40"> / {limit_bytes_str}</span>
          </div>

          <div class="mt-4 rounded-xl border border-white/[0.05] sub-box px-3 pt-3 pb-1.5">
            <p class="flex items-center gap-1.5 text-[10px] text-white/35 mb-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>
              روند مصرف
            </p>
            <svg viewBox="0 0 300 64" class="w-full h-14" preserveAspectRatio="none">
              <defs>
                <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#60a5fa" stop-opacity="0.35"/>
                  <stop offset="100%" stop-color="#60a5fa" stop-opacity="0"/>
                </linearGradient>
              </defs>
              <path d="M0,64 L{svg_points} L300,64 Z" fill="url(#trendFill)"/>
              <path d="M{svg_points}" fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>

          <div class="mt-4 flex items-center justify-between text-[11px] text-white/40 flex-wrap gap-2">
            <span class="inline-flex items-center gap-1.5">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
              باقی‌مانده: <b class="text-white/70 font-bold">{remaining_value_escaped}</b>
            </span>
            <span class="inline-flex items-center gap-1.5">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 3v3M16 3v3"/></svg>
              زمان: <b class="text-white/70 font-bold">{expiry_remaining_escaped}</b>
            </span>

          </div>
        </div>
      </div>
    </div>

    <div class="rounded-[22px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-7">
      <div class="flex items-center gap-2.5">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="text-white/35"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
        <p class="text-[10px] font-extrabold tracking-widest uppercase text-white/30">Service</p>
      </div>
      <div class="mt-4 divide-y divide-white/[0.06]">
        <div class="flex items-center justify-between py-3 first:pt-0">
          <span class="inline-flex items-center gap-2 text-[11px] text-white/45">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 3v3M16 3v3"/></svg>
            انقضا
          </span>
          <span class="text-xs font-extrabold">{expiry_display_escaped}</span>
        </div>
        <div class="flex items-center justify-between py-3">
          <span class="inline-flex items-center gap-2 text-[11px] text-white/45">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14 0"/><path d="M8.5 16a6 6 0 0 1 7 0"/><path d="M12 20h.01"/></svg>
            IP Limit
          </span>
          <span class="text-xs font-extrabold">{ip_limit_escaped}</span>
        </div>
        <div class="flex items-center justify-between py-3">
          <span class="inline-flex items-center gap-2 text-[11px] text-white/45">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2"/><rect x="2" y="9" width="20" height="8" rx="2"/><path d="M6 17v2M18 17v2"/></svg>
            Connection
          </span>
          <span class="text-xs font-extrabold">{connection_limit_escaped}</span>
        </div>
        <div class="flex items-center justify-between py-3 last:pb-0">
          <span class="inline-flex items-center gap-2 text-[11px] text-white/45">
            <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg>
            Speed
          </span>
          <span class="text-xs font-extrabold">{speed_limit_escaped}</span>
        </div>
      </div>
    </div>

  </section>

  <!-- Stats -->
  <section class="grid grid-cols-2 md:grid-cols-4 gap-3.5 sm:gap-4 md:gap-5">

    <div class="rounded-2xl border dynamic-card backdrop-blur-xl p-4 sm:p-5 hover:border-emerald-400/20 transition-colors duration-200">
      <div class="w-9 h-9 rounded-xl grid place-items-center bg-emerald-400/10 border border-emerald-400/20 text-emerald-300">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 9l-5 5-3-3-4 4"/></svg>
      </div>
      <p class="mt-4 text-[11px] text-white/45">مصرف فعلی</p>
      <p class="mt-1 text-[14px] sm:text-[15px] font-black text-emerald-300 break-words">{used_bytes_str}</p>
    </div>

    <div class="rounded-2xl border dynamic-card backdrop-blur-xl p-4 sm:p-5 hover:border-amber-400/20 transition-colors duration-200">
      <div class="w-9 h-9 rounded-xl grid place-items-center bg-amber-400/10 border border-amber-400/20 text-amber-300">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
      </div>
      <p class="mt-4 text-[11px] text-white/45">باقی‌مانده</p>
      <p class="mt-1 text-[14px] sm:text-[15px] font-black text-amber-300 break-words">{remaining_value_escaped}</p>
    </div>

    <div class="rounded-2xl border dynamic-card backdrop-blur-xl p-4 sm:p-5 hover:border-blue-400/20 transition-colors duration-200">
      <div class="w-9 h-9 rounded-xl grid place-items-center bg-blue-400/10 border border-blue-400/20 text-blue-300">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 4v16M4 9h16"/></svg>
      </div>
      <p class="mt-4 text-[11px] text-white/45">اتصالات فعال</p>
      <p class="mt-1 text-[14px] sm:text-[15px] font-black text-blue-300 break-words">{len(unique_ips_for_uuid(uid))}</p>
    </div>

    <div class="rounded-2xl border dynamic-card backdrop-blur-xl p-4 sm:p-5 hover:border-purple-400/20 transition-colors duration-200">
      <div class="w-9 h-9 rounded-xl grid place-items-center bg-purple-400/10 border border-purple-400/20 text-purple-300">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l9 4.5v6c0 5-3.6 8.7-9 9.5-5.4-.8-9-4.5-9-9.5v-6L12 2z"/></svg>
      </div>
      <p class="mt-4 text-[11px] text-white/45">زمان باقی‌مانده</p>
      <p class="mt-1 text-[14px] sm:text-[15px] font-black text-purple-300 break-words">{expiry_remaining_escaped}</p>
    </div>

  </section>

  <!-- Technical details -->
  <section class="rounded-[22px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-7">
    <div class="flex items-center justify-between gap-3 mb-5">
      <p class="flex items-center gap-2 text-sm font-black">
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="text-white/40"><path d="M4 21v-7M4 10V3M12 21v-11M12 6V3M20 21v-5M20 12V3"/><path d="M1 14h6M9 8h6M17 16h6"/></svg>
        جزئیات فنی
      </p>
      <p class="text-[11px] text-white/40">Configuration Details</p>
    </div>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">Protocol</p>
        <p class="mt-2 text-[11px] font-medium text-purple-300 tracking-wide" dir="ltr" style="font-family:ui-monospace,Consolas,monospace">{protocol_escaped}</p>
      </div>
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">Fingerprint</p>
        <p class="mt-2 text-[11px] font-medium text-purple-300 tracking-wide" dir="ltr" style="font-family:ui-monospace,Consolas,monospace">{fingerprint_escaped}</p>
      </div>
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">IP Limit</p>
        <p class="mt-2 text-xs font-bold text-white/85">{ip_limit_escaped}</p>
      </div>
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">Connection Limit</p>
        <p class="mt-2 text-xs font-bold text-white/85">{connection_limit_escaped}</p>
      </div>
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">Speed Limit</p>
        <p class="mt-2 text-xs font-bold text-white/85">{speed_limit_escaped}</p>
      </div>
      <div class="rounded-2xl border border-white/[0.06] sub-box p-4">
        <p class="text-[11px] text-white/45">تاریخ انقضا</p>
        <p class="mt-2 text-xs font-bold text-white/85">{expiry_display_escaped}</p>
      </div>
    </div>
  </section>

  <!-- Links -->
  <section class="rounded-[22px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-7">
    <div class="flex items-center justify-between gap-3 mb-5">
      <p class="flex items-center gap-2 text-sm font-black">
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="text-white/40"><path d="M10 13a5 5 0 0 0 7.07 0l2.83-2.83a5 5 0 0 0-7.07-7.07L11.5 4.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-2.83 2.83a5 5 0 0 0 7.07 7.07l1.41-1.41"/></svg>
        لینک‌های سرویس
      </p>
      <p class="text-[11px] text-white/40">Copy / Import</p>
    </div>

    <div class="space-y-3">
      <div class="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 rounded-2xl border border-white/[0.06] sub-box p-4 hover:border-purple-400/25 transition-colors duration-200">
        <div class="min-w-0 flex-1">
          <p class="text-[11px] font-extrabold text-white/45 tracking-wide">VLESS</p>
          <p id="vlessLinkText" class="mt-1.5 text-[11px] text-purple-300 break-all leading-6" dir="ltr" style="font-family:ui-monospace,Consolas,monospace">{vless_url_escaped}</p>
        </div>
        <button id="vlessCopyBtn" type="button" onclick="pxCopy('vlessLinkText','vlessCopyBtn')"
          class="copy-btn shrink-0 self-start sm:self-center inline-flex items-center gap-1.5 text-[11px] font-bold text-white/60 px-3.5 py-2 rounded-xl bg-white/[0.05] border border-white/10 hover:bg-white/[0.1] hover:text-white transition-colors duration-200">
          <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
          <span>کپی</span>
        </button>
      </div>

      <div class="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 rounded-2xl border border-white/[0.06] sub-box p-4 hover:border-purple-400/25 transition-colors duration-200">
        <div class="min-w-0 flex-1">
          <p class="text-[11px] font-extrabold text-white/45 tracking-wide">SUBSCRIPTION</p>
          <p id="subLinkText" class="mt-1.5 text-[11px] text-purple-300 break-all leading-6" dir="ltr" style="font-family:ui-monospace,Consolas,monospace">{sub_url_escaped}</p>
        </div>
        <button id="subCopyBtn" type="button" onclick="pxCopy('subLinkText','subCopyBtn')"
          class="copy-btn shrink-0 self-start sm:self-center inline-flex items-center gap-1.5 text-[11px] font-bold text-white/60 px-3.5 py-2 rounded-xl bg-white/[0.05] border border-white/10 hover:bg-white/[0.1] hover:text-white transition-colors duration-200">
          <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
          <span>کپی</span>
        </button>
      </div>
    </div>
  </section>

  <!-- Downloads -->
  <section class="rounded-[22px] border dynamic-card backdrop-blur-2xl p-5 sm:p-6 md:p-7">
    <div class="flex items-center justify-between gap-3 mb-5">
      <p class="flex items-center gap-2 text-sm font-black">
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" class="text-white/40"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
        دانلود برنامه‌ها
      </p>
      <p class="text-[11px] text-white/40">Official Releases</p>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3.5">
      <a href="https://github.com/2dust/v2rayNG/releases/latest" target="_blank" rel="noopener noreferrer"
         class="flex items-center gap-3 rounded-2xl border border-white/10 sub-box p-4 hover:border-blue-400/25 transition-colors duration-200">
        <div class="w-10 h-10 shrink-0 rounded-xl grid place-items-center bg-blue-400/10 border border-blue-400/20 text-blue-300 font-black text-[11px]">NG</div>
        <div class="min-w-0">
          <p class="text-xs font-extrabold">v2rayNG</p>
          <p class="mt-0.5 text-[10px] text-white/40">Android</p>
        </div>
      </a>
      <a href="https://github.com/2dust/v2rayN/releases/latest" target="_blank" rel="noopener noreferrer"
         class="flex items-center gap-3 rounded-2xl border border-white/10 sub-box p-4 hover:border-blue-400/25 transition-colors duration-200">
        <div class="w-10 h-10 shrink-0 rounded-xl grid place-items-center bg-blue-400/10 border border-blue-400/20 text-blue-300 font-black text-[11px]">N</div>
        <div class="min-w-0">
          <p class="text-xs font-extrabold">v2rayN</p>
          <p class="mt-0.5 text-[10px] text-white/40">Windows / macOS / Linux</p>
        </div>
      </a>
      <a href="https://github.com/hiddify/hiddify-app/releases/latest" target="_blank" rel="noopener noreferrer"
         class="flex items-center gap-3 rounded-2xl border border-white/10 sub-box p-4 hover:border-blue-400/25 transition-colors duration-200">
        <div class="w-10 h-10 shrink-0 rounded-xl grid place-items-center bg-blue-400/10 border border-blue-400/20 text-blue-300 font-black text-[11px]">H</div>
        <div class="min-w-0">
          <p class="text-xs font-extrabold">Hiddify</p>
          <p class="mt-0.5 text-[10px] text-white/40">Android / Windows / macOS / Linux</p>
        </div>
      </a>
    </div>
  </section>

  <!-- Footer AGN021G -->
  <div class="rounded-2xl border border-emerald-400/15 bg-emerald-400/[0.05] p-4 text-center text-xs text-white/45 space-y-2">
    <div>ساخته شده توسط <b class="text-emerald-300">AGN021G</b></div>
    <div class="flex flex-wrap justify-center gap-3">
      <a href="https://t.me/AGN021G" target="_blank" rel="noopener" class="text-emerald-300 hover:underline">پشتیبانی</a>
      <a href="https://t.me/AGN021GCHAT" target="_blank" rel="noopener" class="text-emerald-300 hover:underline">گروه</a>
      <a href="https://t.me/AGN021G1388" target="_blank" rel="noopener" class="text-emerald-300 hover:underline">کانال</a>
    </div>
  </div>

</div>

<!-- QR Code Modal Popup -->
<div id="qrModal" class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md hidden">
  <div class="w-full max-w-sm rounded-[24px] border border-white/15 bg-[#0b0c14] p-6 text-center shadow-2xl relative">
    <button type="button" onclick="closeQrModal()" class="absolute top-4 left-4 w-8 h-8 rounded-full bg-white/5 border border-white/10 grid place-items-center text-white/60 hover:text-white">✕</button>
    <p class="text-sm font-black text-white/90 mb-2">QR Code اسکن کانفیگ</p>
    <p class="text-[11px] text-white/40 mb-4">برای اتصال سریع با گوشی موبایل</p>
    <div id="qrcodeContainer" class="bg-white p-4 rounded-2xl inline-block mx-auto mb-4 border border-white/10"></div>
    <p id="qrModalText" class="text-[10px] text-purple-300 break-all max-h-16 overflow-y-auto px-2" dir="ltr"></p>
  </div>
</div>

<script>
const vlessUrlData = "{vless_url}";

// Theme toggle logic with localStorage support (2 themes total)
function toggleTheme() {{
  const body = document.body;
  body.classList.toggle('theme-lighter');
  const isLighter = body.classList.contains('theme-lighter');
  localStorage.setItem('px_theme', isLighter ? 'lighter' : 'dark');
}}

// Initialize saved theme on load
(function() {{
  if (localStorage.getItem('px_theme') === 'lighter') {{
    document.body.classList.add('theme-lighter');
  }}
}})();

function openQrModal() {{
  var modal = document.getElementById('qrModal');
  var container = document.getElementById('qrcodeContainer');
  var txtEl = document.getElementById('qrModalText');
  container.innerHTML = "";
  txtEl.textContent = vlessUrlData;
  modal.classList.remove('hidden');
  try {{
    var typeNumber = 0;
    var errorCorrectionLevel = 'L';
    var qr = qrcode(typeNumber, errorCorrectionLevel);
    qr.addData(vlessUrlData);
    qr.make();
    container.innerHTML = qr.createImgTag(5, 8);
  }} catch (e) {{
    container.innerHTML = "<p class='text-xs text-black'>خطا در تولید QR Code</p>";
  }}
}}

function closeQrModal() {{
  document.getElementById('qrModal').classList.add('hidden');
}}

document.getElementById('qrModal').addEventListener('click', function(e) {{
  if (e.target === this) closeQrModal();
}});

function pxCopy(textId, btnId) {{
  var el = document.getElementById(textId);
  var btn = document.getElementById(btnId);
  if (!el || !btn) return;
  var text = el.textContent.textContext || el.textContent.trim();
  var done = function() {{
    var original = btn.getAttribute('data-original');
    if (!original) {{
      original = btn.innerHTML;
      btn.setAttribute('data-original', original);
    }}
    btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg><span>کپی شد</span>';
    btn.classList.add('text-emerald-300','border-emerald-400/30','bg-emerald-400/10');
    setTimeout(function() {{
      btn.innerHTML = original;
      btn.classList.remove('text-emerald-300','border-emerald-400/30','bg-emerald-400/10');
    }}, 1700);
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(done).catch(function() {{ fallbackCopy(text, done); }});
  }} else {{
    fallbackCopy(text, done);
  }}
}}
function fallbackCopy(text, cb) {{
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {{ document.execCommand('copy'); }} catch (e) {{}}
  document.body.removeChild(ta);
  if (cb) cb();
}}
</script>
</body>
</html>"""
    return HTMLResponse(info_html)
# ============================================================
# SUB GROUP API
# ============================================================

@app.post("/api/subs")
async def create_sub_api(
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON نامعتبر است",
        )

    sub_id, sub = await create_sub_group(
        name=body.get(
            "name",
            "گروه جدید",
        ),
        desc=body.get(
            "desc",
            "",
        ),
        password=body.get(
            "password",
            "",
        ),
    )

    host = get_host(request)

    return {
        "sub_id":
            sub_id,

        **sub,

        "password_hash":
            None,

        "public_url":
            (
                f"https://{host}"
                f"/sub-group/{sub['uuid_key']}"
            ),

        "sub_url":
            (
                f"https://{host}"
                f"/sub-group/{sub['uuid_key']}"
            ),
    }


@app.get("/api/subs")
async def list_subs_api(
    request: Request,
    _=Depends(require_auth),
):

    host = get_host(request)

    async with SUBS_LOCK:
        snapshot_subs = dict(SUBS)

    async with LINKS_LOCK:
        snapshot_links = dict(LINKS)

    result = []

    for sid, sub in snapshot_subs.items():

        link_ids = sub.get(
            "link_ids",
            [],
        )

        active_count = sum(
            1
            for lid in link_ids
            if is_link_allowed(
                snapshot_links.get(
                    lid
                )
            )
        )

        total_used = sum(
            snapshot_links[lid].get("used_bytes", 0)
            for lid in link_ids
            if lid in snapshot_links
        )
        total_limit = int(sub.get("limit_bytes") or 0)
        if total_limit <= 0:
            _lims = [
                int(snapshot_links[lid].get("limit_bytes", 0) or 0)
                for lid in link_ids
                if lid in snapshot_links
            ]
            _pos = [x for x in _lims if x > 0]
            if _pos and len(set(_pos)) == 1 and len(_pos) == len(_lims):
                total_limit = _pos[0]
            else:
                total_limit = sum(_lims)
        expiries = []
        protocols = set()
        for lid in link_ids:
            link = snapshot_links.get(lid)
            if not link:
                continue
            if link.get("expires_at"):
                expiries.append(str(link.get("expires_at")))
            protocols.add(link.get("protocol") or DEFAULT_PROTOCOL)
        group_expiry = None
        if expiries:
            try:
                group_expiry = min(expiries, key=lambda x: datetime.fromisoformat(x))
            except Exception:
                group_expiry = expiries[0]

        result.append(
            {
                "sub_id": sid,
                **sub,
                "password_hash": None,
                "has_password": sub.get("password_hash") is not None,
                "links_count": len(link_ids),
                "active_count": active_count,
                "total_used_bytes": total_used,
                "total_used_fmt": fmt_bytes(total_used),
                "total_limit_bytes": total_limit,
                "total_limit_fmt": fmt_bytes(total_limit) if total_limit else "∞",
                "usage_fmt": (
                    f"{fmt_bytes(total_used)}/{fmt_bytes(total_limit)}"
                    if total_limit > 0
                    else f"{fmt_bytes(total_used)}/∞"
                ),
                "expires_at": group_expiry,
                "protocols": sorted(protocols),
                "public_url": f"https://{host}/sub-group/{sub['uuid_key']}",
                "sub_url": f"https://{host}/sub-group/{sub['uuid_key']}",
            }
        )

    result.sort(
        key=lambda item:
            item.get(
                "created_at",
                "",
            ),
        reverse=True,
    )

    return {
        "subs": result
    }


@app.patch("/api/subs/{sub_id}")
async def update_sub_api(
    sub_id: str,
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON نامعتبر است",
        )

    async with SUBS_LOCK:

        if sub_id not in SUBS:
            raise HTTPException(
                status_code=404,
                detail="sub not found",
            )

        sub = SUBS[sub_id]

        if "name" in body:
            sub["name"] = str(
                body["name"]
            )[:60]

        if "desc" in body:
            sub["desc"] = str(
                body["desc"]
            )[:200]

        if "password" in body:

            password = str(
                body.get(
                    "password",
                    "",
                )
            ).strip()

            sub["password_hash"] = (
                hash_password(password)
                if password
                else None
            )

        if "link_ids" in body:

            sub["link_ids"] = list(
                body["link_ids"]
            )

    await save_state()

    return {
        "ok": True
    }


@app.delete("/api/subs/{sub_id}")
async def delete_sub_api(
    sub_id: str,
    _=Depends(require_auth),
):

    name = await remove_sub_group(
        sub_id
    )

    if name is None:
        raise HTTPException(
            status_code=404,
            detail="sub not found",
        )

    return {
        "ok": True,
        "deleted": sub_id,
    }



@app.post("/api/subs/{sub_id}/disable")
async def disable_sub_links(sub_id: str, _=Depends(require_auth)):
    async with SUBS_LOCK:
        sub = SUBS.get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="sub not found")
        ids = list(sub.get("link_ids") or [])
    n = 0
    for lid in ids:
        if await set_link_active(lid, False):
            n += 1
    log_activity("sub", f"کاربر/ساب غیرفعال شد ({n} کانفیگ)", "warn")
    return {"ok": True, "disabled": n}


@app.post("/api/subs/{sub_id}/enable")
async def enable_sub_links(sub_id: str, _=Depends(require_auth)):
    async with SUBS_LOCK:
        sub = SUBS.get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="sub not found")
        ids = list(sub.get("link_ids") or [])
    n = 0
    for lid in ids:
        if await set_link_active(lid, True):
            n += 1
    log_activity("sub", f"کاربر/ساب فعال شد ({n} کانفیگ)", "ok")
    return {"ok": True, "enabled": n}


@app.post("/api/subs/{sub_id}/reset-usage")
async def reset_sub_usage(sub_id: str, _=Depends(require_auth)):
    async with SUBS_LOCK:
        sub = SUBS.get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="sub not found")
        ids = list(sub.get("link_ids") or [])
    n = 0
    async with LINKS_LOCK:
        for lid in ids:
            if lid in LINKS:
                LINKS[lid]["used_bytes"] = 0
                n += 1
    await save_state()
    log_activity("sub", f"مصرف ساب ریست شد ({n} کانفیگ)", "ok")
    return {"ok": True, "reset": n}


@app.post("/api/subs/{sub_id}/extend")
async def extend_sub(sub_id: str, request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    days = safe_int((body or {}).get("days", 30), minimum=1, maximum=3650)
    async with SUBS_LOCK:
        sub = SUBS.get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="sub not found")
        ids = list(sub.get("link_ids") or [])
    n = 0
    now = datetime.now()
    async with LINKS_LOCK:
        for lid in ids:
            link = LINKS.get(lid)
            if not link:
                continue
            base = now
            exp = link.get("expires_at")
            if exp:
                try:
                    base = datetime.fromisoformat(str(exp))
                    if base < now:
                        base = now
                except Exception:
                    base = now
            link["expires_at"] = (base + timedelta(days=days)).isoformat()
            n += 1
    await save_state()
    log_activity("sub", f"انقضای ساب {days} روز تمدید شد ({n} کانفیگ)", "ok")
    return {"ok": True, "extended": n, "days": days}


@app.post("/api/subs/{sub_id}/set-quota")
async def set_sub_quota(sub_id: str, request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    limit_value = safe_float(body.get("limit_value", 0))
    limit_unit = str(body.get("limit_unit", "GB") or "GB").upper()
    limit_bytes = 0 if limit_value <= 0 else parse_size_to_bytes(limit_value, limit_unit)
    ip_limit = body.get("ip_limit", None)
    speed_value = body.get("speed_limit_value", None)
    async with SUBS_LOCK:
        sub = SUBS.get(sub_id)
        if not sub:
            raise HTTPException(status_code=404, detail="sub not found")
        ids = list(sub.get("link_ids") or [])
    n = 0
    async with LINKS_LOCK:
        for lid in ids:
            link = LINKS.get(lid)
            if not link:
                continue
            if "limit_value" in body or "limit_unit" in body:
                link["limit_bytes"] = limit_bytes
            if ip_limit is not None:
                link["ip_limit"] = max(0, safe_int(ip_limit, minimum=0))
            if speed_value is not None:
                sv = safe_float(speed_value)
                link["speed_limit_bytes"] = 0 if sv <= 0 else parse_speed_to_bytes(sv, "MBIT")
            n += 1
    await save_state()
    log_activity("sub", f"سهمیه ساب به‌روز شد ({n} کانفیگ)", "ok")
    return {"ok": True, "updated": n}


@app.post("/api/subs/{sub_id}/links")
async def assign_link_to_sub(
    sub_id: str,
    request: Request,
    _=Depends(require_auth),
):

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON نامعتبر است",
        )

    link_id = str(
        body.get(
            "link_id",
            "",
        )
    )

    action = str(
        body.get(
            "action",
            "add",
        )
    )

    if action == "add":

        success = await set_link_sub(
            link_id,
            sub_id,
        )

    else:

        success = await set_link_sub(
            link_id,
            None,
        )

    if not success:
        raise HTTPException(
            status_code=404,
            detail="link or sub not found",
        )

    return {
        "ok": True
    }


# ============================================================
# GROUP SUB
# ============================================================

@app.get("/sub-group/{uuid_key}")
async def sub_group_subscription(
    uuid_key: str,
    request: Request,
):
    """One link: browser sees usage page; v2ray/clash gets subscription body."""

    async with SUBS_LOCK:

        sub = next(
            (
                item
                for item
                in SUBS.values()
                if item.get(
                    "uuid_key"
                ) == uuid_key
            ),
            None,
        )

    if not sub:
        raise HTTPException(
            status_code=404,
            detail="not found",
        )

    if sub.get(
        "password_hash"
    ):

        password = (
            request.query_params.get(
                "pw",
                "",
            )
        )

        if (
            hash_password(password)
            != sub["password_hash"]
        ):

            raise HTTPException(
                status_code=403,
                detail="wrong password",
            )

    # Same URL → HTML for humans
    if client_wants_html(request):
        return HTMLResponse(PUBLIC_SUB_HTML)

    host = get_host(request)

    async with LINKS_LOCK:

        lines = []

        for link_id in sub.get(
            "link_ids",
            [],
        ):

            link = LINKS.get(
                link_id
            )

            if (
                link
                and is_link_allowed(
                    link
                )
            ):

                lines.append(
                    vless_link_for_link(
                        link,
                        link_id,
                        host,
                    )
                )

    content = (
        base64
        .b64encode(
            "\n".join(
                lines
            ).encode()
        )
        .decode()
    )

    total_used = 0
    total_limit = 0
    expiries = []
    valid_ids = list(sub.get("link_ids", []))

    async with LINKS_LOCK:
        for link_id in valid_ids:
            link = LINKS.get(link_id)
            if not link or not is_link_allowed(link):
                continue
            total_used += int(link.get("used_bytes", 0) or 0)
            total_limit += int(link.get("limit_bytes", 0) or 0)
            if link.get("expires_at"):
                expiries.append(str(link.get("expires_at")))

    # For a group subscription, expose aggregate usage/expiry in standard headers.
    group_limit = total_limit if total_limit > 0 else 0
    group_expiry = None
    if expiries:
        try:
            group_expiry = min(
                expiries,
                key=lambda x: datetime.fromisoformat(x)
            )
        except Exception:
            group_expiry = expiries[0]

    group_volume_text = (
        f"{fmt_bytes(total_used)}/{fmt_bytes(group_limit)}"
        if group_limit > 0
        else f"{fmt_bytes(total_used)}/∞"
    )
    group_expiry_text = group_expiry or "∞"
    group_title = (
        f"0.0.0.0 | {group_volume_text} | {group_expiry_text} | "
        f"{sub['name']} | پشتیبانی"
    )
    headers = subscription_metadata_headers(
        total_used,
        group_limit,
        group_expiry,
        host,
        f"https://{host}/public-sub/{uuid_key}",
        group_title,
    )

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


# ============================================================
# PUBLIC GROUP
# ============================================================

PUBLIC_SUB_HTML = r"""
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>اشتراک شما</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07070f;--card:rgba(255,255,255,.06);--border:rgba(255,255,255,.1);
  --t1:#f1f5f9;--t2:rgba(255,255,255,.65);--t3:rgba(255,255,255,.4);
  --accent:#6366f1;--accent2:#22d3ee;--ok:#34d399;
}
body{
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:24px 16px;font-family:Vazirmatn,system-ui,sans-serif;color:var(--t1);
  background:var(--bg);overflow-x:hidden;position:relative;
}
.bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.bg .orb{position:absolute;border-radius:50%;filter:blur(80px);opacity:.45;animation:float 18s ease-in-out infinite}
.bg .o1{width:420px;height:420px;background:#6366f1;top:-10%;right:-8%}
.bg .o2{width:360px;height:360px;background:#22d3ee;bottom:-15%;left:-10%;animation-delay:-6s}
.bg .o3{width:280px;height:280px;background:#a855f7;top:40%;left:30%;opacity:.25;animation-delay:-12s}
@keyframes float{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(30px,-40px) scale(1.08)}66%{transform:translate(-25px,20px) scale(.95)}}
.grid-overlay{position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.03) 1px,transparent 1px);
  background-size:48px 48px;mask-image:radial-gradient(ellipse at center,black 20%,transparent 75%)}
.wrap{position:relative;z-index:1;width:100%;max-width:520px}
.card{
  background:var(--card);border:1px solid var(--border);border-radius:28px;
  padding:28px 22px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
  box-shadow:0 25px 60px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.06);
  animation:cardIn .7s cubic-bezier(.22,1,.36,1) both;
}
@keyframes cardIn{from{opacity:0;transform:translateY(28px) scale(.96)}to{opacity:1;transform:none}}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;
  background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.35);
  color:#a5b4fc;font-size:11px;font-weight:700;margin-bottom:16px}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 10px var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
h1{font-size:22px;font-weight:800;margin-bottom:6px}
.sub{color:var(--t2);font-size:13px;line-height:1.8;margin-bottom:18px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.stat{background:rgba(0,0,0,.25);border:1px solid var(--border);border-radius:16px;padding:12px 14px}
.stat .l{font-size:11px;color:var(--t3);margin-bottom:4px}
.stat .v{font-size:15px;font-weight:800}
.bar-wrap{margin:4px 0 16px}
.bar-lab{display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-bottom:6px}
.bar{height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}
.bar > i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6366f1,#22d3ee);width:0%;transition:width .8s cubic-bezier(.22,1,.36,1)}
.chart-box{background:rgba(0,0,0,.2);border:1px solid var(--border);border-radius:16px;padding:12px;margin-bottom:16px}
.chart-box .t{font-size:12px;font-weight:700;color:#c7d2fe;margin-bottom:8px}
.url-box{background:rgba(0,0,0,.28);border:1px solid var(--border);border-radius:16px;padding:12px 14px;margin-bottom:14px}
.url-box .lab{font-size:11px;color:var(--t3);margin-bottom:6px}
.url-box .url{font-size:11px;direction:ltr;text-align:left;word-break:break-all;color:#93c5fd;font-family:ui-monospace,monospace;line-height:1.6}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:13px 16px;border-radius:14px;border:none;font-family:inherit;font-size:14px;font-weight:700;cursor:pointer;transition:.2s}
.btn-p{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;box-shadow:0 10px 30px rgba(99,102,241,.35)}
.btn-p:hover{filter:brightness(1.08);transform:translateY(-1px)}
.sec-title{font-size:13px;font-weight:800;margin:18px 0 10px;color:#e2e8f0}
.os-row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.os-btn{
  padding:12px 6px;border-radius:14px;border:1px solid var(--border);background:rgba(0,0,0,.25);
  color:var(--t2);font-family:inherit;font-size:11px;font-weight:700;cursor:pointer;transition:.2s;text-align:center
}
.os-btn:hover{border-color:rgba(99,102,241,.45);color:#fff}
.os-btn.on{background:rgba(99,102,241,.2);border-color:rgba(99,102,241,.55);color:#c7d2fe;box-shadow:0 0 0 1px rgba(99,102,241,.2)}
.os-btn .ic{font-size:18px;display:block;margin-bottom:4px}
.apps{display:none;flex-direction:column;gap:8px;animation:fadeUp .35s both}
.apps.show{display:flex}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.app{
  display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:14px;
  background:rgba(0,0,0,.28);border:1px solid var(--border);text-decoration:none;color:inherit;transition:.2s
}
.app:hover{border-color:rgba(34,211,238,.4);background:rgba(34,211,238,.08)}
.app .name{font-size:13px;font-weight:700}
.app .desc{font-size:11px;color:var(--t3);margin-top:2px}
.app .go{margin-right:auto;font-size:11px;color:#67e8f9;font-weight:700}
.foot{margin-top:16px;text-align:center;font-size:11px;color:var(--t3)}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);opacity:0;
  background:#1e1b4b;color:#fff;padding:10px 18px;border-radius:12px;font-size:13px;font-weight:600;
  transition:.25s;z-index:50;border:1px solid rgba(99,102,241,.4)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:420px){.os-row{grid-template-columns:repeat(2,1fr)}.stats{grid-template-columns:1fr 1fr}}

/* ===== Responsive: phone → tablet → desktop → TV ===== */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
img,canvas,svg{max-width:100%;height:auto}
.wrap{width:100%;max-width:min(560px,100%)}
.card{width:100%}
.btn,.os-btn,.app{min-height:44px;-webkit-tap-highlight-color:transparent}
.os-btn{touch-action:manipulation}
.url-box .url{overflow-wrap:anywhere;word-break:break-word}
@media(max-width:480px){
  body{padding:12px 10px;align-items:flex-start;padding-top:16px}
  .card{padding:20px 14px;border-radius:22px}
  h1{font-size:18px}
  .sub{font-size:12px;margin-bottom:14px}
  .stats{grid-template-columns:1fr 1fr;gap:8px}
  .stat{padding:10px 12px;border-radius:14px}
  .stat .v{font-size:13px}
  .os-row{grid-template-columns:repeat(2,1fr);gap:8px}
  .os-btn{padding:14px 8px;font-size:12px}
  .os-btn .ic{font-size:20px}
  .app{padding:12px;gap:10px}
  .app .name{font-size:13px}
  .chart-box{padding:10px}
  #usageChart{max-height:140px!important}
  .btn{padding:14px 16px;font-size:14px}
  .foot{font-size:10px}
}
@media(min-width:481px) and (max-width:768px){
  body{padding:20px 16px}
  .wrap{max-width:560px}
  .os-row{grid-template-columns:repeat(4,1fr)}
  .stats{grid-template-columns:1fr 1fr}
}
@media(min-width:769px){
  body{padding:32px 24px}
  .wrap{max-width:580px}
  .card{padding:32px 28px}
  h1{font-size:24px}
}
@media(min-width:1200px){
  .wrap{max-width:640px}
  .card{padding:36px 32px;border-radius:32px}
  h1{font-size:26px}
  .stats{gap:14px}
  .stat .v{font-size:16px}
}
/* large TV / ultra-wide: keep content centered, readable, not stretched full-bleed */
@media(min-width:1600px){
  body{padding:48px}
  .wrap{max-width:720px}
  .card{padding:40px 36px}
  h1{font-size:28px}
  .sub{font-size:15px}
  .stat .v{font-size:18px}
  .btn{font-size:16px;padding:16px}
  .os-btn{font-size:13px;padding:16px 10px}
  .app .name{font-size:15px}
}
/* landscape phone */
@media(max-height:480px) and (orientation:landscape){
  body{align-items:flex-start;padding:10px}
  .card{padding:16px}
  .bg .orb{opacity:.25}
  .chart-box{display:none}
}
/* reduce motion */
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}
}
</style>

</head>
<body>
<div class="bg"><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>
<div class="grid-overlay"></div>
<div class="wrap">
  <div class="card">
    <div class="badge"><span class="dot"></span> اشتراک فعال</div>
    <h1 id="subTitle">اشتراک شما</h1>
    <p class="sub">همین لینک را در کلاینت وارد کنید. در مرورگر وضعیت و مصرف را می‌بینید.</p>
    <div class="stats">
      <div class="stat"><div class="l">وضعیت</div><div class="v" id="stStatus">—</div></div>
      <div class="stat"><div class="l">تعداد کانفیگ</div><div class="v" id="stCount">—</div></div>
      <div class="stat"><div class="l">مصرف</div><div class="v" id="stUsage" style="direction:ltr;unicode-bidi:isolate;text-align:left">—</div></div>
      <div class="stat"><div class="l">انقضا</div><div class="v" id="stExp">—</div></div>
    </div>
    <div class="bar-wrap">
      <div class="bar-lab"><span>میزان مصرف</span><span id="barPct">—</span></div>
      <div class="bar"><i id="barFill"></i></div>
    </div>
    <div class="chart-box">
      <div class="t">نمودار مصرف کانفیگ‌ها (GB)</div>
      <canvas id="usageChart" width="480" height="160" style="width:100%;max-height:160px;direction:ltr"></canvas>
    </div>
    <div class="url-box">
      <div class="lab">لینک Subscription</div>
      <div class="url" id="subUrl">در حال بارگذاری...</div>
    </div>
    <button class="btn btn-p" type="button" onclick="copySub()">کپی لینک اشتراک</button>

    <div class="sec-title">دانلود نرم‌افزار · سیستم‌عامل خود را انتخاب کنید</div>
    <div class="os-row">
      <button type="button" class="os-btn" data-os="windows" onclick="showOs('windows')"><span class="ic">🪟</span>ویندوز</button>
      <button type="button" class="os-btn" data-os="mac" onclick="showOs('mac')"><span class="ic"></span>مک</button>
      <button type="button" class="os-btn" data-os="android" onclick="showOs('android')"><span class="ic">🤖</span>اندروید</button>
      <button type="button" class="os-btn" data-os="ios" onclick="showOs('ios')"><span class="ic">📱</span>iOS</button>
    </div>
    <div id="appsWindows" class="apps">
      <a class="app" href="https://github.com/2dust/v2rayN/releases" target="_blank" rel="noopener"><div><div class="name">v2rayN</div><div class="desc">پرکاربرد · ویندوز</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://github.com/hiddify/hiddify-app/releases" target="_blank" rel="noopener"><div><div class="name">Hiddify</div><div class="desc">ساده · پشتیبانی ساب</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://github.com/MatsuriDayo/nekoray/releases" target="_blank" rel="noopener"><div><div class="name">Nekoray</div><div class="desc">پیشرفته · ویندوز/لینوکس</div></div><span class="go">دانلود</span></a>
    </div>
    <div id="appsMac" class="apps">
      <a class="app" href="https://apps.apple.com/app/streisand/id6450534064" target="_blank" rel="noopener"><div><div class="name">Streisand</div><div class="desc">مک / اپ‌استور</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://apps.apple.com/app/v2box-v2ray-client/id6446814690" target="_blank" rel="noopener"><div><div class="name">V2Box</div><div class="desc">مک و آیفون</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://github.com/hiddify/hiddify-app/releases" target="_blank" rel="noopener"><div><div class="name">Hiddify</div><div class="desc">مک · از گیت‌هاب</div></div><span class="go">دانلود</span></a>
    </div>
    <div id="appsAndroid" class="apps">
      <a class="app" href="https://github.com/2dust/v2rayNG/releases" target="_blank" rel="noopener"><div><div class="name">v2rayNG</div><div class="desc">اندروید · محبوب</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://github.com/hiddify/hiddify-app/releases" target="_blank" rel="noopener"><div><div class="name">Hiddify</div><div class="desc">اندروید · ساده</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://play.google.com/store/apps/details?id=com.v2ray.ang" target="_blank" rel="noopener"><div><div class="name">v2rayNG (Play)</div><div class="desc">گوگل‌پلی</div></div><span class="go">دانلود</span></a>
    </div>
    <div id="appsIos" class="apps">
      <a class="app" href="https://apps.apple.com/app/streisand/id6450534064" target="_blank" rel="noopener"><div><div class="name">Streisand</div><div class="desc">آیفون · اپ‌استور</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://apps.apple.com/app/v2box-v2ray-client/id6446814690" target="_blank" rel="noopener"><div><div class="name">V2Box</div><div class="desc">آیفون · رایگان</div></div><span class="go">دانلود</span></a>
      <a class="app" href="https://apps.apple.com/app/shadowrocket/id932747118" target="_blank" rel="noopener"><div><div class="name">Shadowrocket</div><div class="desc">آیفون · پولی</div></div><span class="go">دانلود</span></a>
    </div>
    <div id="tgProxyBox" style="display:none;margin-top:18px">
      <div class="sec-title">پروکسی تلگرام</div>
      <p style="font-size:12px;color:var(--t3);line-height:1.7;margin-bottom:10px">یکی از پروکسی‌ها را باز کنید تا در تلگرام اضافه شود.</p>
      <div id="tgProxyList" style="display:flex;flex-direction:column;gap:8px"></div>
    </div>
    <div class="sec-title" style="margin-top:18px">پشتیبانی AGN021G</div>
    <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">
      <a class="app" href="https://t.me/AGN021G" target="_blank" rel="noopener"><div><div class="name">پشتیبانی</div><div class="desc">@AGN021G · پاسخ سریع</div></div><span class="go">باز کردن</span></a>
      <a class="app" href="https://t.me/AGN021GCHAT" target="_blank" rel="noopener"><div><div class="name">گروه کاربران</div><div class="desc">AGN021GCHAT</div></div><span class="go">عضویت</span></a>
      <a class="app" href="https://t.me/AGN021G1388" target="_blank" rel="noopener"><div><div class="name">کانال اطلاع‌رسانی</div><div class="desc">AGN021G1388</div></div><span class="go">عضویت</span></a>
    </div>
    <p style="font-size:11px;color:var(--t3);text-align:center;margin-top:8px">ساخته شده توسط <b>AGN021G</b></p>
    <div class="foot">این صفحه را ذخیره کنید · لینک را در یکی از نرم‌افزارهای بالا Import کنید</div>
  </div>
</div>
<div class="toast" id="toast">کپی شد</div>
<script>
const subUrl = location.origin + location.pathname + location.search;
document.getElementById('subUrl').textContent = subUrl;
const key = location.pathname.split('/').filter(Boolean).pop();

function toast(m){
  const t=document.getElementById('toast');
  t.textContent=m;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),1800);
}
async function copySub(){
  try{
    if(navigator.clipboard&&window.isSecureContext) await navigator.clipboard.writeText(subUrl);
    else{const a=document.createElement('textarea');a.value=subUrl;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
    toast('لینک کپی شد ✓');
  }catch(e){toast('کپی نشد')}
}
function showOs(os){
  document.querySelectorAll('.os-btn').forEach(b=>b.classList.toggle('on', b.dataset.os===os));
  document.querySelectorAll('.apps').forEach(a=>a.classList.remove('show'));
  const map={windows:'appsWindows',mac:'appsMac',android:'appsAndroid',ios:'appsIos'};
  const el=document.getElementById(map[os]);
  if(el) el.classList.add('show');
}
function fmtGB(b){
  b=Number(b)||0; const gb=b/(1024**3);
  if(b===0) return '0 GB';
  if(gb<0.01) return gb.toFixed(4)+' GB';
  if(gb<10) return gb.toFixed(3)+' GB';
  return gb.toFixed(2)+' GB';
}
function drawUsageChart(links){
  const canvas=document.getElementById('usageChart');
  if(!canvas)return;
  const dpr=window.devicePixelRatio||1;
  const W=canvas.clientWidth||480, H=160;
  canvas.width=Math.floor(W*dpr); canvas.height=Math.floor(H*dpr);
  const ctx=canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const items=(links||[]).slice().sort((a,b)=>(b.used_bytes||0)-(a.used_bytes||0)).slice(0,8);
  if(!items.length){
    ctx.fillStyle='rgba(148,163,184,.7)'; ctx.font='12px Vazirmatn,sans-serif'; ctx.textAlign='center';
    ctx.fillText('هنوز مصرفی ثبت نشده', W/2, H/2); return;
  }
  const vals=items.map(x=>(Number(x.used_bytes)||0)/(1024**3));
  const maxV=Math.max(0.001, ...vals);
  const pad={t:10,r:8,b:28,l:8};
  const n=items.length, gw=(W-pad.l-pad.r)/n, barW=Math.max(6,Math.min(26,gw*0.55));
  const grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  grad.addColorStop(0,'#818cf8'); grad.addColorStop(1,'#22d3ee');
  vals.forEach((v,i)=>{
    const h=((H-pad.t-pad.b)*v)/maxV;
    const x=pad.l+i*gw+(gw-barW)/2, y=H-pad.b-h;
    ctx.fillStyle=grad;
    ctx.beginPath();
    const r=5;
    ctx.moveTo(x,y+r); ctx.arcTo(x,y,x+barW,y,r); ctx.arcTo(x+barW,y,x+barW,y+h,r);
    ctx.lineTo(x+barW,H-pad.b); ctx.lineTo(x,H-pad.b); ctx.closePath(); ctx.fill();
    ctx.fillStyle='rgba(148,163,184,.75)'; ctx.font='9px Vazirmatn,sans-serif'; ctx.textAlign='center';
    const lab=String(items[i].label||items[i].uuid||'').slice(0,6);
    ctx.fillText(lab, x+barW/2, H-pad.b+12);
  });
}
(async function loadMeta(){
  try{
    const r=await fetch('/api/public/sub/'+key);
    if(!r.ok)return;
    const d=await r.json();
    if(d.locked){document.getElementById('subTitle').textContent='قفل شده';return}
    if(d.name) document.getElementById('subTitle').textContent=d.name;
    document.getElementById('stStatus').textContent=d.active===false?'غیرفعال':'فعال';
    document.getElementById('stCount').textContent=d.count!=null?d.count:'—';
    document.getElementById('stUsage').textContent=d.usage||'—';
    document.getElementById('stExp').textContent=d.expiry||'∞';
    const used=Number(d.total_used_bytes||0);
    const lim=Number(d.total_limit_bytes||0);
    let pct=0;
    if(lim>0) pct=Math.min(100, (used/lim)*100);
    document.getElementById('barFill').style.width=(lim>0?pct:0)+'%';
    document.getElementById('barPct').textContent=lim>0?(pct.toFixed(1)+'% · '+fmtGB(used)+' / '+fmtGB(lim)):(fmtGB(used)+' / ∞');
    drawUsageChart(d.links||[]);
    try{
      const mr=await fetch('/api/public/mtproto');
      if(mr.ok){
        const m=await mr.json();
        const box=document.getElementById('tgProxyBox');
        const list=document.getElementById('tgProxyList');
        if(m&&m.ok&&m.enabled&&m.proxies&&m.proxies.length&&box&&list){
          box.style.display='block';
          list.innerHTML=m.proxies.map(function(p,i){
            const href=p.https_link||'#';
            const name=p.name||('پروکسی '+(i+1));
            return '<a class="app" href="'+href+'" target="_blank" rel="noopener"><div><div class="name">'+name+'</div><div class="desc">MTProto · باز کردن در تلگرام</div></div><span class="go">اتصال</span></a>'
              +'<button type="button" class="btn" style="background:rgba(255,255,255,.08);color:#e2e8f0;border:1px solid var(--border);margin-bottom:8px" data-link="'+href+'" onclick="copyTgProxy(this.dataset.link)">کپی لینک '+name+'</button>';
          }).join('');
        }
      }
    }catch(e){}

  }catch(e){}
})();
function copyTgProxy(u){
  if(!u){toast('آماده نیست');return}
  (async()=>{
    try{
      if(navigator.clipboard&&window.isSecureContext) await navigator.clipboard.writeText(u);
      else{const a=document.createElement('textarea');a.value=u;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
      toast('لینک پروکسی تلگرام کپی شد');
    }catch(e){toast('کپی نشد')}
  })();
}
// auto-pick OS
(function(){
  const ua=navigator.userAgent||'';
  let os='android';
  if(/Windows/i.test(ua)) os='windows';
  else if(/Mac OS|Macintosh/i.test(ua) && !/iPhone|iPad/i.test(ua)) os='mac';
  else if(/iPhone|iPad|iPod/i.test(ua)) os='ios';
  else if(/Android/i.test(ua)) os='android';
  showOs(os);
})();
</script>
</body>
</html>
"""




@app.get(
    "/p/{uuid_key}",
    response_class=HTMLResponse,
)
async def public_sub_page(
    uuid_key: str,
    request: Request,
):
    # legacy alias → one canonical sub URL
    qs = ("?" + str(request.url.query)) if request.url.query else ""
    return RedirectResponse(f"/sub-group/{uuid_key}{qs}", status_code=302)


@app.get("/api/public/sub/{uuid_key}")
async def public_sub_data(
    uuid_key: str,
    request: Request,
):

    async with SUBS_LOCK:

        entry = next(
            (
                (
                    sid,
                    item,
                )

                for sid, item
                in SUBS.items()

                if item.get(
                    "uuid_key"
                ) == uuid_key
            ),
            None,
        )

    if not entry:
        raise HTTPException(
            status_code=404,
            detail="not found",
        )

    _, sub = entry

    has_password = (
        sub.get(
            "password_hash"
        ) is not None
    )

    if has_password:

        password = (
            request
            .query_params
            .get(
                "pw",
                "",
            )
        )

        if (
            hash_password(password)
            != sub[
                "password_hash"
            ]
        ):

            return JSONResponse(
                {
                    "locked": True,
                    "name":
                        sub["name"],
                }
            )

    host = get_host(request)

    async with LINKS_LOCK:
        snapshot = dict(LINKS)

    links_out = []

    active_connections = 0

    for link_id in sub.get(
        "link_ids",
        [],
    ):

        link = snapshot.get(
            link_id
        )

        if not link:
            continue

        allowed = is_link_allowed(
            link
        )

        connection_count = sum(
            1
            for item in connections.values()
            if item.get("uuid") == link_id
        )

        active_connections += (
            connection_count
        )

        links_out.append(
            {
                "uuid":
                    link_id,

                "label":
                    link.get(
                        "label"
                    ),

                "active":
                    allowed,

                "protocol":
                    link.get(
                        "protocol",
                        DEFAULT_PROTOCOL,
                    ),

                "used_bytes":
                    link.get(
                        "used_bytes",
                        0,
                    ),

                "used_fmt":
                    fmt_bytes(
                        link.get(
                            "used_bytes",
                            0,
                        )
                    ),

                "limit_bytes":
                    link.get(
                        "limit_bytes",
                        0,
                    ),

                "limit_fmt":
                    (
                        "∞"
                        if not link.get(
                            "limit_bytes",
                            0,
                        )
                        else fmt_bytes(
                            link[
                                "limit_bytes"
                            ]
                        )
                    ),

                "expires_at":
                    link.get(
                        "expires_at"
                    ),

                "vless_link":
                    vless_link_for_link(
                        link,
                        link_id,
                        host,
                    ),

                "sub_url":
                    (
                        f"https://{host}"
                        f"/sub/{link_id}"
                    ),

                "info_url":
                    (
                        f"https://{host}"
                        f"/info/{link_id}"
                    ),

                "connections":
                    connection_count,

                "ip_limit":
                    link.get(
                        "ip_limit",
                        0,
                    ),

                "speed_limit_bytes":
                    link.get(
                        "speed_limit_bytes",
                        0,
                    ),

                "connection_limit":
                    link.get(
                        "connection_limit",
                        0,
                    ),
            }
        )

    total_used = sum(
        int(item.get("used_bytes") or 0)
        for item in links_out
    )
    # prefer sub-level limit; else same-limit-on-all = group; else sum
    total_limit = int(sub.get("limit_bytes") or 0)
    if total_limit <= 0:
        link_limits = [int(item.get("limit_bytes") or 0) for item in links_out]
        positive = [x for x in link_limits if x > 0]
        if positive and len(set(positive)) == 1 and len(positive) == len(link_limits):
            total_limit = positive[0]
        else:
            total_limit = sum(link_limits)
    expiries = [
        item.get("expires_at") for item in links_out if item.get("expires_at")
    ]
    group_expiry = None
    if expiries:
        try:
            group_expiry = min(expiries, key=lambda x: datetime.fromisoformat(str(x)))
        except Exception:
            group_expiry = expiries[0]
    active_links = sum(1 for item in links_out if item.get("active"))
    usage_str = (
        f"{fmt_bytes(total_used)}/{fmt_bytes(total_limit)}"
        if total_limit > 0
        else f"{fmt_bytes(total_used)}/∞"
    )
    expiry_str = "∞"
    if group_expiry:
        try:
            exp_dt = datetime.fromisoformat(str(group_expiry))
            expiry_str = exp_dt.strftime("%Y/%m/%d")
        except Exception:
            expiry_str = str(group_expiry)[:10]

    return {
        "locked": False,
        "name": sub["name"],
        "desc": sub.get("desc", ""),
        "sub_url": f"https://{host}/sub-group/{uuid_key}",
        "active_connections": active_connections,
        "total_used_fmt": fmt_bytes(total_used),
        "total_used_bytes": int(total_used),
        "total_limit_bytes": int(total_limit),
        "count": len(links_out),
        "active": active_links > 0,
        "usage": usage_str,
        "expiry": expiry_str,
        "support": SUPPORT_USERNAME,
        "links": links_out,
    }




@app.post("/api/mix-sub")
async def mix_subscription(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON نامعتبر")
    ids = body.get("link_ids") or []
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(status_code=400, detail="حداقل ۲ کانفیگ انتخاب کنید")
    if len(ids) > 40:
        raise HTTPException(status_code=400, detail="حداکثر ۴۰ کانفیگ")
    host = get_host(request)
    lines = []
    used_names = set()
    total_used = 0
    total_limit = 0
    labels = []
    async with LINKS_LOCK:
        for lid in ids:
            link = LINKS.get(lid)
            if not link or not is_link_allowed(link):
                continue
            labels.append(str(link.get("label") or lid[:8]))
            total_used += int(link.get("used_bytes", 0) or 0)
            total_limit += int(link.get("limit_bytes", 0) or 0)
            name = random_config_name(used_names)
            used_names.add(name)
            _h = (link.get("endpoint_host") or host)
            lines.append(generate_vless_link(
                lid, _h, remark=name,
                protocol=link.get("protocol", DEFAULT_PROTOCOL),
                fingerprint=link.get("fingerprint", DEFAULT_FINGERPRINT),
                alpn=link.get("alpn"),
                port=link.get("port", DEFAULT_PORT),
                security=link.get("security") or None,
            ))
    if not lines:
        raise HTTPException(status_code=400, detail="هیچ کانفیگ معتبری انتخاب نشده")
    # stats first line
    vol = f"{fmt_bytes(total_used)}/{fmt_bytes(total_limit)}" if total_limit > 0 else f"{fmt_bytes(total_used)}/∞"
    mix_label = "Mix-" + random_config_name()[:6]
    stats = f"{mix_label} | {vol} | {len(lines)} configs"
    first = generate_vless_link(ids[0], "127.0.0.1", remark=stats, protocol="vless-ws")
    content = base64.b64encode(("\n".join([first] + lines)).encode()).decode()
    # store as a sub group for reuse
    sub_id, sub = await create_sub_group(name=mix_label, desc="مخلوط‌سازی کانفیگ‌ها")
    async with SUBS_LOCK:
        if sub_id in SUBS:
            SUBS[sub_id]["link_ids"] = list(ids)
    await save_state()
    return {
        "ok": True,
        "sub_url": f"https://{host}/sub-group/{sub['uuid_key']}",
        "name": mix_label,
        "count": len(lines),
        "content_preview": stats,
    }


@app.get("/api/categories")
async def list_categories(_=Depends(require_auth)):
    items = [{**cat, "id": cid} for cid, cat in CATEGORIES.items()]
    items.sort(key=lambda x: int(x.get("number", 0)))
    return {"categories": items}

@app.post("/api/categories")
async def create_category(request: Request, _=Depends(require_auth)):
    if len(CATEGORIES) >= 50:
        raise HTTPException(status_code=400, detail="حداکثر ۵۰ گروه")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON نامعتبر")
    name = str(body.get("name") or "دسته جدید").strip()[:40]
    used = {int(x.get("number", 0)) for x in CATEGORIES.values()}
    num = 0
    while num in used:
        num += 1
    cid = str(num)
    limit_value = safe_float(body.get("limit_value", 0))
    limit_unit = str(body.get("limit_unit") or "GB").upper()
    limit_bytes = 0 if limit_value <= 0 else parse_size_to_bytes(limit_value, limit_unit)
    speed_value = safe_float(body.get("speed_limit_value", 0))
    speed_bytes = 0 if speed_value <= 0 else parse_speed_to_bytes(speed_value, "MBIT")
    raw_clean = body.get("clean_ips") or ""
    if isinstance(raw_clean, list):
        clean_ips = [str(x).strip() for x in raw_clean if str(x).strip()]
    else:
        clean_ips = [x.strip() for x in str(raw_clean).replace(",", "\n").splitlines() if x.strip()]
    record = {
        "id": cid, "name": name, "number": num,
        "limit_bytes": limit_bytes,
        "expires_days": safe_int(body.get("expires_days", 0), minimum=0),
        "connection_limit": safe_int(body.get("connection_limit", 0), minimum=0),
        "speed_limit_bytes": speed_bytes,
        "ip_limit": safe_int(body.get("ip_limit", 0), minimum=0),
        "clean_ips": clean_ips,
        "random_name": bool(body.get("random_name", False)),
        "single_user": bool(body.get("single_user", False)),
        "created_at": datetime.now().isoformat(),
    }
    CATEGORIES[cid] = record
    await save_state()
    return {"ok": True, **record}


@app.patch("/api/categories/{cid}")
async def update_category(cid: str, request: Request, _=Depends(require_auth)):
    if cid not in CATEGORIES:
        raise HTTPException(status_code=404, detail="یافت نشد")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON نامعتبر")
    cat = CATEGORIES[cid]
    if "name" in body:
        cat["name"] = str(body.get("name") or cat["name"]).strip()[:40]
    if "limit_value" in body:
        lv = safe_float(body.get("limit_value", 0))
        unit = str(body.get("limit_unit") or "GB").upper()
        cat["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, unit)
    if "expires_days" in body:
        cat["expires_days"] = safe_int(body.get("expires_days", 0), minimum=0)
    if "connection_limit" in body:
        cat["connection_limit"] = safe_int(body.get("connection_limit", 0), minimum=0)
    if "speed_limit_value" in body:
        sv = safe_float(body.get("speed_limit_value", 0))
        cat["speed_limit_bytes"] = 0 if sv <= 0 else parse_speed_to_bytes(sv, "MBIT")
    if "ip_limit" in body:
        cat["ip_limit"] = safe_int(body.get("ip_limit", 0), minimum=0)
    if "clean_ips" in body:
        raw = body.get("clean_ips") or ""
        if isinstance(raw, list):
            cat["clean_ips"] = [str(x).strip() for x in raw if str(x).strip()]
        else:
            cat["clean_ips"] = [x.strip() for x in str(raw).replace(",", "\n").splitlines() if x.strip()]
    if "random_name" in body:
        cat["random_name"] = bool(body.get("random_name"))
    if "single_user" in body:
        cat["single_user"] = bool(body.get("single_user"))
    await save_state()
    return {"ok": True, **cat}

@app.delete("/api/categories/{cid}")
async def delete_category(cid: str, _=Depends(require_auth)):
    if cid not in CATEGORIES:
        raise HTTPException(status_code=404, detail="یافت نشد")
    del CATEGORIES[cid]
    for link in LINKS.values():
        if str(link.get("category_id")) == cid:
            link["category_id"] = "0"
    await save_state()
    return {"ok": True}

# ============================================================
# STATS
# ============================================================

@app.get("/stats")
async def get_stats(
    _=Depends(require_auth),
):

    async with LINKS_LOCK:
        snapshot = dict(LINKS)

    return {
        "service":
            APP_NAME,

        "version":
            APP_VERSION,

        "active_connections":
            len(connections),

        "total_traffic_mb":
            round(
                stats[
                    "total_bytes"
                ]
                / (
                    1024 ** 2
                ),
                2,
            ),

        "total_traffic_bytes":
            stats[
                "total_bytes"
            ],

        "total_requests":
            stats[
                "total_requests"
            ],

        "total_errors":
            stats[
                "total_errors"
            ],

        "uptime":
            uptime(),

        "timestamp":
            datetime.now().isoformat(),

        "hourly":
            dict(
                hourly_traffic
            ),

        "recent_errors":
            list(
                error_logs
            )[-10:],

        "links_count":
            len(snapshot),

        "active_links":
            sum(
                1
                for link
                in snapshot.values()
                if is_link_allowed(
                    link
                )
            ),

        "expired_links":
            sum(
                1
                for link
                in snapshot.values()
                if is_link_expired(
                    link
                )
            ),

        "subs_count":
            len(SUBS),

        "total_traffic_gb":
            round(stats["total_bytes"] / (1024 ** 3), 4),

        "top_links":
            sorted(
                [
                    {
                        "id": uid[:8],
                        "label": str(link.get("label") or uid[:8])[:24],
                        "used_bytes": int(link.get("used_bytes") or 0),
                        "limit_bytes": int(link.get("limit_bytes") or 0),
                    }
                    for uid, link in snapshot.items()
                ],
                key=lambda x: x["used_bytes"],
                reverse=True,
            )[:10],
    }


@app.get("/api/activity")
async def get_activity(
    _=Depends(require_auth),
):

    return {
        "logs":
            list(
                activity_logs
            )[-150:]
    }


# ============================================================
# CONNECTIONS
# ============================================================

@app.get("/api/connections")
async def get_connections(
    _=Depends(require_auth),
):

    async with LINKS_LOCK:
        snapshot = dict(LINKS)

    grouped = {}

    for connection in connections.values():

        ip = connection.get(
            "ip",
            "نامشخص",
        )

        link = snapshot.get(
            connection.get(
                "uuid"
            )
        )

        label = (
            link.get(
                "label"
            )
            if link
            else "نامشخص"
        )

        group = grouped.get(ip)

        if group is None:

            group = {
                "ip":
                    ip,

                "sessions":
                    0,

                "bytes":
                    0,

                "labels":
                    set(),

                "transports":
                    set(),

                "first_connected_at":
                    connection.get(
                        "connected_at"
                    ),

                "last_connected_at":
                    connection.get(
                        "connected_at"
                    ),
            }

            grouped[ip] = group

        group["sessions"] += 1

        group["bytes"] += int(
            connection.get(
                "bytes",
                0,
            )
            or 0
        )

        group["labels"].add(
            label
        )

        group["transports"].add(
            connection.get(
                "transport",
                DEFAULT_PROTOCOL,
            )
        )

    result = []

    for group in grouped.values():

        result.append(
            {
                "ip":
                    group["ip"],

                "sessions":
                    group["sessions"],

                "labels":
                    sorted(
                        group["labels"]
                    ),

                "label":
                    (
                        " · ".join(
                            sorted(
                                group["labels"]
                            )
                        )
                        if group["labels"]
                        else "نامشخص"
                    ),

                "transports":
                    sorted(
                        group["transports"]
                    ),

                "bytes":
                    group["bytes"],

                "bytes_fmt":
                    fmt_bytes(
                        group["bytes"]
                    ),

                "connected_at":
                    group[
                        "first_connected_at"
                    ],

                "last_connected_at":
                    group[
                        "last_connected_at"
                    ],
            }
        )

    result.sort(
        key=lambda item:
            item.get(
                "last_connected_at"
            )
            or "",
        reverse=True,
    )

    return {
        "connections":
            result,

        "count":
            len(result),

        "raw_count":
            len(connections),
    }


# ============================================================
# OPTIONAL EXISTING PROJECT MODULES
# ============================================================

# ============================================================
# IMPORTANT:
# DO NOT REPLACE THIS VLESS CORE.
# ============================================================

try:

    from relay_vless import (
        RELAY_BUF,
        parse_vless_header,
        check_and_use,
        relay_ws_to_tcp,
        relay_tcp_to_ws,
        websocket_tunnel,
    )

    app.add_api_websocket_route(
        "/ws/{uuid}",
        websocket_tunnel,
    )

    logger.info(
        "VLESS relay loaded."
    )

except Exception as exc:

    logger.warning(
        "VLESS relay module unavailable: %s",
        exc,
    )


# ============================================================
# XHTTP
# ============================================================

try:

    from xhttp_siz10 import (
        router as xhttp_router
    )

    app.include_router(
        xhttp_router
    )

    logger.info(
        "XHTTP module loaded."
    )

except Exception as exc:

    logger.warning(
        "XHTTP module unavailable: %s",
        exc,
    )



# ============================================================

@app.get("/api/me")
async def api_me_info(request: Request, token=Depends(require_auth)):
    meta = get_session_meta(token)
    return {
        "ok": True,
        "role": meta.get("role"),
        "username": meta.get("username"),
        "permissions": meta.get("permissions") or {p: True for p in ALL_PERMS},
        "uptime": uptime(),
    }


@app.get("/api/admins")
async def api_admins_list(token=Depends(require_perm("admins"))):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    out = []
    for aid, a in ADMIN_ACCOUNTS.items():
        out.append({
            "id": aid,
            "username": a.get("username"),
            "label": a.get("label"),
            "limit_bytes": int(a.get("limit_bytes") or 0),
            "used_bytes": int(a.get("used_bytes") or 0),
            "expires_at": a.get("expires_at"),
            "active": bool(a.get("active", True)),
            "blocked": bool(a.get("blocked")),
            "permissions": a.get("permissions") or {},
            "created_at": a.get("created_at"),
            "valid": admin_is_valid(a),
        })
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"admins": out}


@app.post("/api/admins")
async def api_admins_create(request: Request, token=Depends(require_perm("admins"))):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="JSON نامعتبر")
    username = str(body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    repeat = str(body.get("repeat_password") or body.get("confirm") or "")
    if not username or len(username) < 3:
        raise HTTPException(400, detail="نام کاربری حداقل ۳ کاراکتر")
    if not username.isalnum():
        raise HTTPException(400, detail="نام کاربری فقط حروف و عدد انگلیسی")
    if username in ("owner", "admin", "root"):
        raise HTTPException(400, detail="این نام کاربری رزرو شده است")
    if find_admin_by_username(username)[0]:
        raise HTTPException(400, detail="نام کاربری تکراری است")
    if len(password) < 6:
        raise HTTPException(400, detail="رمز حداقل ۶ کاراکتر")
    if password != repeat:
        raise HTTPException(400, detail="تکرار رمز یکسان نیست")
    limit_value = safe_float(body.get("limit_value", 0))
    limit_unit = str(body.get("limit_unit") or "GB").upper()
    limit_bytes = 0 if limit_value <= 0 else parse_size_to_bytes(limit_value, limit_unit)
    days = safe_int(body.get("expires_days", 0), minimum=0)
    expires_at = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    perms_in = body.get("permissions") or {}
    permissions = {p: bool(perms_in.get(p, False)) for p in ALL_PERMS}
    rec = default_admin_record(username, password, limit_bytes=limit_bytes, expires_at=expires_at, permissions=permissions, label=body.get("label") or username)
    ADMIN_ACCOUNTS[rec["id"]] = rec
    await save_state()
    log_activity("admin", f"اکانت ادمین «{username}» ساخته شد", "ok")
    return {"ok": True, "id": rec["id"], "username": username}


@app.patch("/api/admins/{aid}")
async def api_admins_patch(aid: str, request: Request, token=Depends(require_perm("admins"))):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    if aid not in ADMIN_ACCOUNTS:
        raise HTTPException(404, detail="یافت نشد")
    body = await request.json()
    a = ADMIN_ACCOUNTS[aid]
    if "blocked" in body:
        a["blocked"] = bool(body["blocked"])
    if "active" in body:
        a["active"] = bool(body["active"])
    if "label" in body:
        a["label"] = str(body["label"])[:40]
    if "permissions" in body and isinstance(body["permissions"], dict):
        a["permissions"] = {p: bool(body["permissions"].get(p, False)) for p in ALL_PERMS}
    if "limit_value" in body:
        lv = safe_float(body.get("limit_value", 0))
        lu = str(body.get("limit_unit") or "GB").upper()
        a["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
    if "expires_days" in body:
        days = safe_int(body.get("expires_days", 0), minimum=0)
        a["expires_at"] = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    if body.get("password"):
        pw = str(body["password"])
        if len(pw) < 6:
            raise HTTPException(400, detail="رمز حداقل ۶ کاراکتر")
        a["password_hash"] = hash_password(pw)
    await save_state()
    log_activity("admin", f"اکانت ادمین «{a.get('username')}» ویرایش شد", "ok")
    return {"ok": True}


@app.delete("/api/admins/{aid}")
async def api_admins_delete(aid: str, token=Depends(require_perm("admins"))):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    a = ADMIN_ACCOUNTS.pop(aid, None)
    if not a:
        raise HTTPException(404, detail="یافت نشد")
    await save_state()
    log_activity("admin", f"اکانت ادمین «{a.get('username')}» حذف شد", "warn")
    return {"ok": True}


NEWS_FILE = Path(__file__).resolve().parent / "news.json"


@app.get("/api/news")
async def api_news(token=Depends(require_auth)):
    try:
        if NEWS_FILE.exists():
            data = json.loads(NEWS_FILE.read_text(encoding="utf-8"))
        else:
            data = {"enabled": False, "title": "", "message": "", "updated_at": ""}
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "enabled": False, "title": "", "message": str(e), "updated_at": ""}




# ============================================================
# BACKUP / RESTORE
# ============================================================





@app.get("/api/mtproto")
async def get_mtproto(_=Depends(require_auth)):
    proxies = list_mtproto_proxies()
    return {
        "ok": True,
        "enabled": bool(MTPROTO_CFG.get("enabled")),
        "proxies": proxies,
        "count": len(proxies),
    }


@app.post("/api/mtproto")
async def set_mtproto(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    if "enabled" in body:
        MTPROTO_CFG["enabled"] = bool(body.get("enabled"))
    if "proxies" in body and isinstance(body.get("proxies"), list):
        cleaned = []
        for item in body["proxies"][:8]:
            if not isinstance(item, dict):
                continue
            srv = str(item.get("server") or "").strip()
            sec = str(item.get("secret") or "").strip()
            if body.get("generate_secret") and not sec:
                sec = generate_mtproto_secret(str(item.get("fake_tls_domain") or body.get("fake_tls_domain") or ""))
            if not srv or not sec:
                continue
            try:
                prt = max(1, min(65535, int(item.get("port") or 443)))
            except Exception:
                prt = 443
            cleaned.append({
                "name": str(item.get("name") or f"پروکسی {len(cleaned)+1}")[:40],
                "server": srv,
                "port": prt,
                "secret": sec,
                "tag": str(item.get("tag") or "").strip(),
            })
        MTPROTO_CFG["proxies"] = cleaned
    elif body.get("generate_secret"):
        # generate for first proxy or create one
        domain = str(body.get("fake_tls_domain") or "").strip()
        sec = generate_mtproto_secret(domain)
        if MTPROTO_CFG.get("proxies"):
            MTPROTO_CFG["proxies"][0]["secret"] = sec
        else:
            MTPROTO_CFG["proxies"] = [{"name": "پروکسی ۱", "server": "", "port": 443, "secret": sec, "tag": ""}]
    await save_state()
    log_activity("sys", f"پروکسی تلگرام: {len(MTPROTO_CFG.get('proxies') or [])} مورد", "ok")
    proxies = list_mtproto_proxies()
    return {"ok": True, "enabled": bool(MTPROTO_CFG.get("enabled")), "proxies": proxies, "count": len(proxies)}


@app.get("/api/public/mtproto")
async def public_mtproto():
    if not MTPROTO_CFG.get("enabled"):
        return {"ok": False, "enabled": False, "proxies": [], "count": 0}
    proxies = list_mtproto_proxies()
    public = []
    for p in proxies:
        public.append({
            "name": p.get("name"),
            "https_link": p.get("https_link"),
            "tg_link": p.get("tg_link"),
            "server": p.get("server"),
            "port": p.get("port"),
        })
    return {"ok": True, "enabled": True, "proxies": public, "count": len(public)}




@app.get("/api/system/update-check")
async def update_check(_=Depends(require_auth)):
    """بررسی نسخه جدید از مخزن GitHub AGN021G"""
    current = APP_VERSION
    github = PANEL_GITHUB
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # VERSION file or main.py APP_VERSION
            r = await client.get(f"{PANEL_GITHUB_RAW}/VERSION")
            remote = None
            notes = ""
            if r.status_code == 200 and r.text.strip():
                remote = r.text.strip().splitlines()[0].strip()
            else:
                r2 = await client.get(f"{PANEL_GITHUB_RAW}/main.py")
                if r2.status_code == 200:
                    import re as _re
                    m = _re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', r2.text[:5000])
                    if m:
                        remote = m.group(1)
            if not remote:
                return {
                    "ok": True,
                    "available": False,
                    "up_to_date": True,
                    "current": current,
                    "remote": current,
                    "github": github,
                    "message": "نسخه ریموت خوانده نشد — مخزن را بررسی کنید",
                }
            up_to_date = remote == current
            if not up_to_date:
                rn = await client.get(f"{PANEL_GITHUB_RAW}/CHANGELOG.md")
                if rn.status_code == 200:
                    notes = rn.text.strip()[:800]
            return {
                "ok": True,
                "available": not up_to_date,
                "up_to_date": up_to_date,
                "current": current,
                "remote": remote,
                "notes": notes,
                "github": github,
            }
    except Exception as exc:
        logger.warning("update-check failed: %s", exc)
        return {
            "ok": False,
            "available": False,
            "up_to_date": False,
            "current": current,
            "github": github,
            "message": f"خطا در بررسی: {exc}",
        }


@app.post("/api/system/update-apply")
async def update_apply(_=Depends(require_auth)):
    """دانلود فایل‌های اصلی پنل از GitHub و جایگزینی روی دیسک"""
    import shutil
    files = ["main.py", "relay_vless.py", "xhttp_siz10.py", "speed_limit.py", "telegram_bot.py", "requirements.txt", "README.md"]
    base = Path(__file__).resolve().parent
    updated = []
    errors = []
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for name in files:
                url = f"{PANEL_GITHUB_RAW}/{name}"
                try:
                    r = await client.get(url)
                    if r.status_code != 200 or not r.content:
                        errors.append(f"{name}: HTTP {r.status_code}")
                        continue
                    target = base / name
                    backup = base / f"{name}.bak"
                    if target.exists():
                        shutil.copy2(target, backup)
                    target.write_bytes(r.content)
                    updated.append(name)
                except Exception as e:
                    errors.append(f"{name}: {e}")
        log_activity("sys", f"آپدیت AGN021G: {len(updated)} فایل", "ok" if updated else "warn")
        if not updated:
            return {"ok": False, "message": "هیچ فایلی دانلود نشد. " + "; ".join(errors[:3])}
        msg = f"{len(updated)} فایل به‌روز شد. سرویس را Restart کنید."
        if errors:
            msg += " خطاها: " + "; ".join(errors[:3])
        return {"ok": True, "message": msg, "updated": updated, "errors": errors}
    except Exception as exc:
        logger.error("update-apply: %s", exc)
        return {"ok": False, "message": str(exc)}


@app.get("/api/network")
async def get_network(request: Request, _=Depends(require_auth)):
    host, port, sec = get_public_endpoint(request)
    return {
        "ok": True,
        "public_host": NETWORK_CFG.get("public_host") or "",
        "public_port": int(NETWORK_CFG.get("public_port") or 0),
        "public_security": NETWORK_CFG.get("public_security") or "tls",
        "prefer_ipv6": bool(NETWORK_CFG.get("prefer_ipv6", True)),
        "effective_host": host,
        "effective_port": port,
        "effective_security": sec,
        "sample": f"{host}:{port} ({sec})",
    }


@app.post("/api/network")
async def set_network(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    host = str((body or {}).get("public_host") or "").strip()
    # allow hostname, domain, ipv4, ipv6
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int((body or {}).get("public_port") or 0)
    except Exception:
        port = 0
    if port < 0 or port > 65535:
        raise HTTPException(status_code=400, detail="پورت نامعتبر")
    security = str((body or {}).get("public_security") or "tls").strip().lower()
    if security not in ("tls", "none"):
        raise HTTPException(status_code=400, detail="security باید tls یا none باشد")
    prefer = (body or {}).get("prefer_ipv6")
    NETWORK_CFG["public_host"] = host
    NETWORK_CFG["public_port"] = port
    NETWORK_CFG["public_security"] = security
    if prefer is not None:
        NETWORK_CFG["prefer_ipv6"] = bool(prefer)
    await save_state()
    log_activity("sys", f"شبکه عمومی: {host or '(auto)'}:{port or 443} / {security}", "ok")
    eh, ep, es = get_public_endpoint(request)
    return {"ok": True, "effective_host": eh, "effective_port": ep, "effective_security": es}


@app.get("/api/panel-path")
async def get_panel_path(request: Request, _=Depends(require_auth)):
    host = get_host(request)
    path = PANEL_PATH
    return {
        "ok": True,
        "path": path,
        "login_url": f"https://{host}/{path}/login",
        "dashboard_url": f"https://{host}/{path}/dashboard",
        "from_env": bool((os.environ.get("PANEL_PATH") or "").strip()),
    }


@app.post("/api/panel-path")
async def set_panel_path(request: Request, _=Depends(require_auth)):
    global PANEL_PATH
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_path = str((body or {}).get("path") or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", new_path or ""):
        raise HTTPException(
            status_code=400,
            detail="مسیر باید ۴ تا ۶۴ کاراکتر و فقط حروف، عدد، - و _ باشد",
        )
    if (os.environ.get("PANEL_PATH") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="PANEL_PATH از Environment تنظیم شده؛ برای تغییر، env را عوض کنید و سرویس را ری‌استارت کنید",
        )
    try:
        PANEL_PATH_FILE.write_text(new_path, encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ذخیره ناموفق: {exc}")
    old = PANEL_PATH
    PANEL_PATH = new_path
    log_activity("sys", f"مسیر مخفی پنل تغییر کرد: /{old} → /{new_path}", "ok")
    host = get_host(request)
    return {
        "ok": True,
        "path": new_path,
        "login_url": f"https://{host}/{new_path}/login",
        "dashboard_url": f"https://{host}/{new_path}/dashboard",
        "message": "ذخیره شد. از لینک جدید وارد شوید.",
    }


@app.get("/api/security/status")
async def security_status(token=Depends(require_auth)):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک")
    now = time.time()
    locked = []
    for ip, until in list(LOGIN_LOCKED_UNTIL.items()):
        if until > now:
            locked.append({"ip": ip, "remaining_sec": int(until - now)})
    return {
        "ok": True,
        "max_attempts": LOGIN_MAX_ATTEMPTS,
        "window_seconds": LOGIN_WINDOW_SECONDS,
        "lockout_seconds": LOGIN_LOCKOUT_SECONDS,
        "locked_ips": locked,
        "tracked_ips": len(LOGIN_FAILURES),
    }


@app.post("/api/security/unlock")
async def security_unlock(request: Request, token=Depends(require_auth)):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک")
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = str((body or {}).get("ip") or "").strip()
    if ip:
        LOGIN_FAILURES.pop(ip, None)
        LOGIN_LOCKED_UNTIL.pop(ip, None)
    else:
        LOGIN_FAILURES.clear()
        LOGIN_LOCKED_UNTIL.clear()
    log_activity("auth", f"رفع مسدودی brute-force ({ip or 'all'})", "ok")
    return {"ok": True}


@app.get("/api/backup/users")
async def backup_users(token=Depends(require_auth)):
    meta = get_session_meta(token)
    # owner always; admin needs settings perm
    if meta.get("role") != "owner":
        if not (meta.get("permissions") or {}).get("settings"):
            raise HTTPException(403, detail="دسترسی ندارید")
    payload = {
        "type": "panel_users_backup",
        "version": APP_VERSION,
        "created_at": datetime.now().isoformat(),
        "links": dict(LINKS),
        "subs": dict(SUBS),
        "categories": dict(CATEGORIES),
        "admin_accounts": dict(ADMIN_ACCOUNTS),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="panel-users-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json"'
        },
    )


@app.get("/api/backup/bot")
async def backup_bot(token=Depends(require_auth)):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        if not (meta.get("permissions") or {}).get("settings"):
            raise HTTPException(403, detail="دسترسی ندارید")
    data = {}
    try:
        if TG_FILE.exists():
            data = json.loads(TG_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    payload = {
        "type": "panel_bot_backup",
        "version": APP_VERSION,
        "created_at": datetime.now().isoformat(),
        "telegram": data,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="panel-bot-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json"'
        },
    )


@app.post("/api/restore/users")
async def restore_users(request: Request, token=Depends(require_auth)):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="فایل JSON نامعتبر")
    if not isinstance(body, dict):
        raise HTTPException(400, detail="فرمت نامعتبر")
    # accept either wrapper or raw state
    links = body.get("links")
    if links is None and body.get("type") == "panel_users_backup":
        raise HTTPException(400, detail="لینک‌ها در بک‌آپ نیست")
    if links is None:
        raise HTTPException(400, detail="فایل بک‌آپ کاربران نیست")
    if not isinstance(links, dict):
        raise HTTPException(400, detail="links نامعتبر")
    mode = str(body.get("mode") or "merge").lower()  # merge | replace
    async with LINKS_LOCK:
        if mode == "replace":
            LINKS.clear()
            SUBS.clear()
            CATEGORIES.clear()
            ADMIN_ACCOUNTS.clear()
        LINKS.update(links)
        if isinstance(body.get("subs"), dict):
            SUBS.update(body["subs"])
        if isinstance(body.get("categories"), dict):
            CATEGORIES.update(body["categories"])
        if isinstance(body.get("admin_accounts"), dict):
            ADMIN_ACCOUNTS.update(body["admin_accounts"])
        for uid, link in list(LINKS.items()):
            if not isinstance(link, dict):
                LINKS.pop(uid, None)
                continue
            link.setdefault("protocol", DEFAULT_PROTOCOL)
            link.setdefault("fingerprint", DEFAULT_FINGERPRINT)
            link.setdefault("used_bytes", 0)
            link.setdefault("active", True)
            link.setdefault("config_count", 1)
    await save_state()
    log_activity("backup", f"بازیابی کاربران ({mode}) — {len(links)} کانفیگ", "ok")
    return {"ok": True, "links": len(LINKS), "subs": len(SUBS), "mode": mode}


@app.post("/api/restore/bot")
async def restore_bot(request: Request, token=Depends(require_auth)):
    meta = get_session_meta(token)
    if meta.get("role") != "owner":
        raise HTTPException(403, detail="فقط مالک پنل")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="فایل JSON نامعتبر")
    tg = body.get("telegram") if isinstance(body, dict) else None
    if tg is None and isinstance(body, dict) and (body.get("token") or body.get("admin_ids") is not None):
        tg = body
    if not isinstance(tg, dict):
        raise HTTPException(400, detail="فایل بک‌آپ ربات نیست")
    # merge with existing
    current = {}
    try:
        if TG_FILE.exists():
            current = json.loads(TG_FILE.read_text(encoding="utf-8"))
    except Exception:
        current = {}
    current.update({k: v for k, v in tg.items() if v is not None})
    TG_FILE.parent.mkdir(parents=True, exist_ok=True)
    TG_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    # try activate
    try:
        from telegram_bot import configure_bot, start_bot, stop_bot, setup_webhook
        await stop_bot()
        configure_bot(current.get("token") or "", current.get("admin_ids") or "")
        host = get_host(request)
        if current.get("webhook") and host and host != "localhost":
            wh = f"https://{host}/telegram/webhook"
            await setup_webhook(wh)
            await start_bot(mode="webhook")
        else:
            await setup_webhook("")
            await start_bot(mode="polling")
    except Exception as exc:
        logger.warning("restore bot activate: %s", exc)
        log_activity("backup", f"بک‌آپ ربات ذخیره شد (فعال‌سازی: {exc})", "warn")
        return {"ok": True, "warning": str(exc)}
    log_activity("backup", "بازیابی تنظیمات ربات انجام شد", "ok")
    return {"ok": True, "message": "ربات بازیابی و فعال شد"}



# TELEGRAM SETTINGS API
# ============================================================

def load_tg_settings():
    try:
        if TG_FILE.exists():
            return json.loads(TG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "admin_ids": os.environ.get("TELEGRAM_ADMIN_IDS", "").strip(),
        "webhook": False,
        "enabled": False,
    }


def save_tg_settings(data: dict):
    TG_FILE.parent.mkdir(parents=True, exist_ok=True)
    TG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/telegram/settings")
async def api_tg_get(_=Depends(require_auth)):
    s = load_tg_settings()
    token = s.get("token") or ""
    masked = (token[:8] + "…" + token[-4:]) if len(token) > 14 else ("••••" if token else "")
    return {
        "token_masked": masked,
        "has_token": bool(token),
        "admin_ids": s.get("admin_ids") or "",
        "webhook": bool(s.get("webhook")),
        "enabled": bool(s.get("enabled")),
    }


@app.post("/api/telegram/settings")
async def api_tg_save(request: Request, _=Depends(require_auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail="invalid json")
    s = load_tg_settings()
    token = str(body.get("token") or "").strip()
    admin_ids = str(body.get("admin_ids") or "").strip()
    use_webhook = bool(body.get("webhook", True))
    if token:
        s["token"] = token
    if admin_ids is not None:
        s["admin_ids"] = admin_ids
    s["webhook"] = use_webhook
    s["enabled"] = True
    save_tg_settings(s)
    # apply runtime
    try:
        from telegram_bot import configure_bot, start_bot, stop_bot, setup_webhook
        await stop_bot()
        configure_bot(s.get("token") or "", s.get("admin_ids") or "")
        host = get_host(request)
        if use_webhook and host and host != "localhost":
            wh = f"https://{host}/telegram/webhook"
            ok = await setup_webhook(wh)
            s["webhook_url"] = wh
            s["webhook_ok"] = bool(ok)
            save_tg_settings(s)
            await start_bot(mode="webhook")
        else:
            await setup_webhook("")  # delete webhook -> polling
            await start_bot(mode="polling")
        log_activity("telegram", "ربات تلگرام پیکربندی و فعال شد", "ok")
        return {"ok": True, "webhook": use_webhook, "message": "ربات فعال شد"}
    except Exception as exc:
        logger.warning("telegram activate error: %s", exc)
        return {"ok": True, "warning": str(exc), "message": "تنظیمات ذخیره شد"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        from telegram_bot import process_update
        data = await request.json()
        await process_update(data)
    except Exception as exc:
        logger.warning("webhook error: %s", exc)
    return {"ok": True}


# ============================================================
# TELEGRAM
# ============================================================

try:

    from telegram_bot import (
        start_bot as _tg_start_bot,
        stop_bot as _tg_stop_bot,
    )

except Exception:

    async def _tg_start_bot():
        return None

    async def _tg_stop_bot():
        return None


@app.on_event("startup")
async def start_optional_telegram():

    try:

        await _tg_start_bot()

        logger.info(
            "Telegram module initialized."
        )

    except Exception as exc:

        logger.warning(
            "Telegram bot disabled/error: %s",
            exc,
        )


# ============================================================
# HTTP PROXY
# ============================================================

_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


@app.api_route(
    "/proxy/{target_url:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "HEAD",
        "OPTIONS",
    ],
)
async def http_proxy(
    target_url: str,
    request: Request,
):

    if not target_url.startswith("http"):
        target_url = (
            "https://"
            + target_url
        )

    if http_client is None:
        raise HTTPException(
            status_code=503,
            detail="HTTP client not ready",
        )

    try:

        body = await request.body()

        headers = {
            key: value
            for key, value
            in request.headers.items()
            if (
                key.lower()
                not in _HOP
            )
            and (
                key.lower()
                != "host"
            )
        }

        response = await http_client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )

        stats["total_bytes"] += len(
            response.content
        )

        stats["total_requests"] += 1

        hourly_traffic[
            now_ir().strftime(
                "%H:00"
            )
        ] += len(
            response.content
        )

        output_headers = {
            key: value
            for key, value
            in response.headers.items()
            if key.lower() not in _HOP
        }

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=output_headers,
        )

    except Exception as exc:

        stats["total_errors"] += 1

        error_logs.append(
            {
                "error":
                    str(exc),

                "url":
                    target_url,

                "time":
                    datetime.now().isoformat(),
            }
        )

        logger.exception(
            "Proxy error: %s",
            target_url,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Proxy error: "
                f"{exc}"
            ),
        )


# ============================================================
# DASHBOARD
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="fa" dir="rtl" id="htmlRoot">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Panel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#06060b;--bg2:#0b0b12;--bg3:#12121c;--card:rgba(18,18,28,.92);--card-b:rgba(255,255,255,.08);
  --accent:#3b82f6;--accent2:#60a5fa;--purple:#8b5cf6;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;
  --t1:#f8fafc;--t2:rgba(248,250,252,.72);--t3:rgba(248,250,252,.42);
  --sb:252px;--sb-c:74px;--radius:18px;--shadow:0 12px 40px rgba(0,0,0,.45);
  --input-bg:rgba(0,0,0,.4);--hover:rgba(59,130,246,.12);
  --glow:0 0 40px rgba(59,130,246,.12);--glass:blur(16px);
}
html.light{
  --bg:#eef1f8;--bg2:#ffffff;--bg3:#f1f4fa;--card:#ffffff;--card-b:rgba(15,23,42,.09);
  --accent:#2563eb;--accent2:#3b82f6;--purple:#7c3aed;--green:#16a34a;--red:#dc2626;--amber:#d97706;
  --t1:#0f172a;--t2:#475569;--t3:#94a3b8;
  --shadow:0 10px 32px rgba(15,23,42,.08);
  --input-bg:#f8fafc;--hover:rgba(37,99,235,.08);
  --glow:0 0 32px rgba(37,99,235,.08);--glass:blur(12px);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%}
body{font-family:'Vazirmatn',sans-serif;background:var(--bg);color:var(--t1);display:flex;min-height:100vh;overflow-x:hidden;transition:background .3s,color .3s;position:relative}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 70% 45% at 15% 20%, rgba(59,130,246,.22), transparent 55%),
    radial-gradient(ellipse 55% 40% at 85% 15%, rgba(168,85,247,.18), transparent 50%),
    radial-gradient(ellipse 50% 45% at 70% 85%, rgba(34,211,238,.12), transparent 50%),
    radial-gradient(ellipse 40% 35% at 20% 80%, rgba(244,114,182,.10), transparent 50%);
  animation:bgShift 22s ease-in-out infinite alternate;
}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:
    linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px);
  background-size:56px 56px;
  mask-image:radial-gradient(ellipse at center, black 25%, transparent 72%);
  animation:gridDrift 40s linear infinite;
}
@keyframes bgShift{
  0%{filter:hue-rotate(0deg) saturate(1);transform:scale(1)}
  50%{filter:hue-rotate(25deg) saturate(1.15);transform:scale(1.05)}
  100%{filter:hue-rotate(-15deg) saturate(1.05);transform:scale(1.02)}
}
@keyframes gridDrift{
  0%{background-position:0 0,0 0}
  100%{background-position:56px 56px,56px 56px}
}
.bg-orbs{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}
.bg-orbs span{position:absolute;border-radius:50%;filter:blur(60px);opacity:.55;animation:orbFloat 16s ease-in-out infinite}
.bg-orbs span:nth-child(1){width:380px;height:380px;background:#3b82f6;top:-8%;right:-5%;animation-delay:0s}
.bg-orbs span:nth-child(2){width:320px;height:320px;background:#a855f7;bottom:-12%;left:-8%;animation-delay:-5s}
.bg-orbs span:nth-child(3){width:260px;height:260px;background:#22d3ee;top:42%;left:38%;opacity:.28;animation-delay:-9s}
.bg-orbs span:nth-child(4){width:200px;height:200px;background:#f472b6;top:18%;left:12%;opacity:.22;animation-delay:-12s}
@keyframes orbFloat{
  0%,100%{transform:translate(0,0) scale(1)}
  33%{transform:translate(28px,-36px) scale(1.08)}
  66%{transform:translate(-22px,18px) scale(.94)}
}
html.light body::before{
  background:
    radial-gradient(ellipse 70% 45% at 15% 20%, rgba(37,99,235,.12), transparent 55%),
    radial-gradient(ellipse 55% 40% at 85% 15%, rgba(124,58,237,.10), transparent 50%),
    radial-gradient(ellipse 50% 45% at 70% 85%, rgba(6,182,212,.08), transparent 50%);
  animation:none;
}
html.light body::after{opacity:.4;animation:none}
html.light .bg-orbs span{opacity:.25;filter:blur(70px)}
.sidebar,.main,.mob-bar,.modal-bg,.toast,.bg-orbs{position:relative}
.sidebar,.main,.mob-bar,.modal-bg,.toast{z-index:1}
.bg-orbs{z-index:0;position:fixed}
.sidebar{z-index:300}.mob-bar{z-index:250}.modal-bg{z-index:500}.toast{z-index:999}
body.en{font-family:'Inter',system-ui,sans-serif}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:var(--t3);border-radius:99px}

.sidebar{position:fixed;right:0;top:0;bottom:0;width:var(--sb);background:var(--bg2);border-left:1px solid var(--card-b);display:flex;flex-direction:column;z-index:300;transition:width .28s cubic-bezier(.4,0,.2,1),transform .28s,background .3s;box-shadow:var(--shadow);backdrop-filter:var(--glass)}
.sidebar.collapsed{width:var(--sb-c)}
.sb-toggle{position:absolute;left:-15px;top:50%;transform:translateY(-50%);width:30px;height:30px;border-radius:8px;background:var(--accent);border:2px solid var(--bg);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:310;box-shadow:0 4px 14px rgba(37,99,235,.4);transition:.2s}
.sb-toggle:hover{filter:brightness(1.1);transform:translateY(-50%) scale(1.05)}
.sb-toggle svg{width:14px;height:14px;transition:transform .28s}
.sidebar.collapsed .sb-toggle svg{transform:rotate(180deg)}
.sb-logo{display:flex;align-items:center;gap:12px;padding:20px 16px;border-bottom:1px solid var(--card-b)}
.sb-logo-icon{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#fff;flex-shrink:0;box-shadow:0 4px 14px rgba(59,130,246,.35)}
.sb-logo-text{overflow:hidden;white-space:nowrap}
.sb-logo-name{font-size:15px;font-weight:800;letter-spacing:-.02em}
.sb-logo-ver{font-size:10px;color:var(--t3);margin-top:2px}
.sidebar.collapsed .sb-logo-text,
.sidebar.collapsed .nav-label,
.sidebar.collapsed .nav-sec,
.sidebar.collapsed .sb-foot span{display:none!important}
.sidebar.collapsed .sb-logo{justify-content:center;padding:16px 8px}
.sidebar.collapsed .sb-logo-icon{margin:0 auto}
.nav{flex:1;overflow-y:auto;padding:10px 0}
.nav-sec{padding:14px 18px 6px;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--t3);font-weight:700}
.nav-item{display:flex;align-items:center;gap:11px;padding:11px 16px;margin:2px 10px;border-radius:12px;color:var(--t3);cursor:pointer;transition:.15s;border:none;background:transparent;width:calc(100% - 20px);font-family:inherit;font-size:13px;font-weight:500}
.nav-item svg{width:18px;height:18px;min-width:18px;min-height:18px;flex-shrink:0;display:block}
.nav-item:hover{background:var(--hover);color:var(--t2)}
.nav-item.on{background:var(--hover);color:var(--accent2);font-weight:700;box-shadow:inset -3px 0 0 var(--accent)}
.sidebar.collapsed .nav-item{justify-content:center;align-items:center;padding:12px 0;margin:3px 10px;width:calc(100% - 20px);gap:0}
.sidebar.collapsed .nav-item svg{margin:0 auto}
.sidebar.collapsed .nav-item.on{box-shadow:none}
.sidebar.collapsed .sb-foot button,.sidebar.collapsed .sb-foot a.btn{padding:10px 0;gap:0}
.sidebar.collapsed .sb-foot button svg,.sidebar.collapsed .sb-foot a.btn svg{margin:0 auto;display:block}
.sb-foot{padding:12px;border-top:1px solid var(--card-b);display:flex;flex-direction:column;gap:7px}
.sb-foot button,.sb-foot a.btn{display:flex;align-items:center;justify-content:center;gap:8px;padding:10px;border-radius:11px;border:1px solid var(--card-b);background:var(--bg3);color:var(--t2);cursor:pointer;font-family:inherit;font-size:12px;width:100%;text-decoration:none;font-weight:600;transition:.15s}
.sb-foot button:hover,.sb-foot a.btn:hover{background:var(--hover);color:var(--t1)}
.sb-foot a.danger{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.2);color:var(--red)}

.main{margin-right:var(--sb);flex:1;min-width:0;padding:28px 24px 60px;transition:margin .28s}
.main.expanded{margin-right:var(--sb-c)}
.page{display:none;animation:fadeIn .28s cubic-bezier(.22,1,.36,1)}
.card{transition:transform .2s ease,box-shadow .2s ease,border-color .2s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 12px 32px rgba(0,0,0,.25)}
.action-card{cursor:pointer}
.metric{transition:transform .2s ease}
.metric:hover{transform:scale(1.02)}
.btn{transition:transform .15s ease,box-shadow .15s ease,opacity .15s}
.btn:hover{filter:brightness(1.06)}
.btn:active{transform:scale(.97)}
.page.on{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px;margin-bottom:22px}
.page-title{font-size:20px;font-weight:800;display:flex;align-items:center;gap:10px;letter-spacing:-.02em}
.page-title svg{width:22px;height:22px;color:var(--accent2)}
.page-sub{font-size:12px;color:var(--t3);margin-top:5px}

.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.metric{background:var(--card);border:1px solid var(--card-b);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);transition:.25s;backdrop-filter:var(--glass)}
.metric:hover{border-color:rgba(59,130,246,.25)}
.metric-label{font-size:11px;color:var(--t3);margin-bottom:8px;display:flex;align-items:center;gap:6px;font-weight:600}
.metric-val{font-size:24px;font-weight:800;letter-spacing:-.03em}
.card{background:var(--card);border:1px solid var(--card-b);border-radius:var(--radius);padding:20px;margin-bottom:14px;box-shadow:var(--shadow);backdrop-filter:var(--glass);transition:border-color .2s,box-shadow .2s}
.card-title{font-size:13px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card-title svg{width:16px;height:16px;color:var(--accent2)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.action-card{cursor:pointer;transition:.2s;border:1px solid var(--card-b)}
.action-card:hover{border-color:rgba(59,130,246,.4);transform:translateY(-2px);box-shadow:0 12px 28px rgba(59,130,246,.12)}
.action-card.purple:hover{border-color:rgba(139,92,246,.45)}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:10px 16px;border-radius:11px;border:1px solid var(--card-b);background:var(--bg3);color:var(--t2);cursor:pointer;font-family:inherit;font-size:12px;font-weight:600;transition:.15s}
.btn:hover{color:var(--t1);border-color:var(--accent)}
.btn-p{background:linear-gradient(135deg,#3b82f6,#6366f1);border:none;color:#fff;box-shadow:0 6px 20px rgba(59,130,246,.35)}
.btn-p:hover{filter:brightness(1.08);color:#fff}
.btn-d{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.25);color:var(--red)}
.btn-sm{padding:7px 11px;font-size:11px;border-radius:9px}
.btn svg{width:15px;height:15px}

.table-wrap{overflow-x:auto;border-radius:14px;border:1px solid var(--card-b)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:right;padding:12px 14px;background:var(--bg3);color:var(--t3);font-weight:700;white-space:nowrap}
td{padding:12px 14px;border-top:1px solid var(--card-b);vertical-align:middle}
tr:hover td{background:var(--hover)}
.ops{display:flex;gap:5px;flex-wrap:wrap;align-items:center}

.range-tabs{display:flex;gap:4px;background:var(--bg3);padding:4px;border-radius:12px;border:1px solid var(--card-b)}
.range-tab{padding:7px 13px;border-radius:9px;font-size:11px;font-weight:700;color:var(--t3);cursor:pointer;border:none;background:transparent;font-family:inherit;transition:.15s}
.range-tab.on{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(37,99,235,.35)}

.field{margin-bottom:14px}
.field label{display:block;font-size:11px;color:var(--t3);margin-bottom:6px;font-weight:700}
.field input,.field select,.field textarea{width:100%;padding:11px 13px;border-radius:11px;border:1px solid var(--card-b);background:var(--input-bg);color:var(--t1);font-family:inherit;font-size:13px;outline:none;transition:.15s}
.field input:focus,.field select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,130,246,.15)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}

.support-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.support-tile{display:flex;align-items:center;gap:14px;padding:20px;background:var(--card);border:1px solid var(--card-b);border-radius:var(--radius);text-decoration:none;color:inherit;transition:.2s;box-shadow:var(--shadow)}
.support-tile:hover{border-color:rgba(59,130,246,.35);transform:translateY(-3px)}
.support-icon{width:48px;height:48px;border-radius:14px;background:var(--hover);display:flex;align-items:center;justify-content:center;flex-shrink:0}
.support-icon svg{width:22px;height:22px;color:var(--accent2)}
.support-label{font-size:11px;color:var(--t3);font-weight:600}
.support-val{font-size:13px;font-weight:700;margin-top:3px}

.log-item{padding:12px 0;border-bottom:1px solid var(--card-b);font-size:12px;display:flex;gap:12px;align-items:flex-start}
.log-time{color:var(--t3);font-size:10px;white-space:nowrap;min-width:72px;font-weight:600}
.log-msg{color:var(--t2);flex:1;line-height:1.5}

.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(6px);z-index:500;display:none;align-items:center;justify-content:center;padding:16px}
.modal-bg.open{display:flex}
.modal{background:var(--bg2);border:1px solid var(--card-b);border-radius:20px;width:min(520px,100%);max-height:90vh;overflow-y:auto;padding:24px;box-shadow:0 24px 64px rgba(0,0,0,.4)}
.modal-title{font-size:17px;font-weight:800;margin-bottom:16px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px;flex-wrap:wrap}
.link-box{background:var(--input-bg);border:1px solid var(--card-b);border-radius:12px;padding:12px;font-size:11px;word-break:break-all;color:var(--t2);margin:8px 0 12px;font-family:ui-monospace,monospace;line-height:1.6;max-height:90px;overflow:auto}

.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(90px);background:var(--bg2);border:1px solid var(--card-b);color:var(--t1);padding:13px 22px;border-radius:14px;font-size:13px;font-weight:600;z-index:999;opacity:0;transition:.3s;pointer-events:none;box-shadow:var(--shadow)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.switch{position:relative;display:inline-block;width:44px;height:26px;vertical-align:middle}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:rgba(148,163,184,.35);border-radius:26px;transition:.2s}
.slider:before{position:absolute;content:"";height:20px;width:20px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 2px 6px rgba(0,0,0,.2)}
.switch input:checked+.slider{background:var(--green)}
.switch input:checked+.slider:before{transform:translateX(18px)}

.mob-bar{display:none;position:fixed;top:0;left:0;right:0;height:56px;background:var(--bg2);border-bottom:1px solid var(--card-b);z-index:250;align-items:center;justify-content:space-between;padding:0 16px;box-shadow:var(--shadow)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:290;display:none}
.overlay.show{display:block}

@media(max-width:900px){
  .sidebar{transform:translateX(100%)}
  .sidebar.open{transform:translateX(0)}
  .sb-toggle{display:none!important}
  .main,.main.expanded{margin-right:0;padding-top:72px}
  .mob-bar{display:flex}
  .metrics{grid-template-columns:1fr 1fr}
  .g2,.form-row{grid-template-columns:1fr}
}
@media(max-width:480px){.metrics{grid-template-columns:1fr}}
.spin{width:36px;height:36px;border:3px solid var(--card-b);border-top-color:var(--accent);border-radius:50%;margin:0 auto;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.conn-badge{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:20px;padding:0 7px;border-radius:8px;font-size:10px;font-weight:800}
.conn-badge.green{background:rgba(34,197,94,.18);color:#4ade80}
.conn-badge.gray{background:rgba(148,163,184,.15);color:#94a3b8}
.conn-badge.orange{background:rgba(245,158,11,.18);color:#fbbf24}
.conn-badge.red{background:rgba(239,68,68,.18);color:#f87171}
.spin{width:36px;height:36px;border:3px solid var(--card-b);border-top-color:var(--accent);border-radius:50%;margin:0 auto;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* extra responsive polish */
@media(max-width:640px){
  .main{padding:12px 10px 88px!important}
  .page-head{flex-direction:column;align-items:stretch;gap:10px}
  .metrics{grid-template-columns:1fr 1fr!important;gap:8px}
  .metric-val{font-size:18px!important}
  .g2{grid-template-columns:1fr!important}
  .card{padding:14px!important}
  .table-wrap{margin:0 -4px;overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{min-width:560px}
  .range-tabs{flex-wrap:wrap}
  canvas{max-width:100%!important}
  #chartHourly,#chartTop,#chartPie{max-height:200px!important}
}
@media(min-width:641px) and (max-width:1024px){
  .metrics{grid-template-columns:repeat(2,1fr)!important}
  .g2{grid-template-columns:1fr 1fr!important}
}
@media(min-width:1400px){
  .main{max-width:1400px}
  .metrics{gap:16px}
  .metric-val{font-size:26px}
}
@media(min-width:1800px){
  .main{padding:28px 40px}
  .page-title{font-size:22px}
}
@media(prefers-reduced-motion:reduce){
  body::before,body::after,.bg-orbs span{animation:none!important}
}
</style>
</head>
<body>
<div class="bg-orbs" aria-hidden="true"><span></span><span></span><span></span><span></span></div>

<div class="mob-bar">
  <div style="font-weight:800;font-size:15px">Panel</div>
  <button class="btn btn-sm" id="mobMenuBtn" aria-label="menu">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="20" height="20"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
  </button>
</div>
<div class="overlay" id="overlay"></div>

<aside class="sidebar" id="sidebar">
  <button class="sb-toggle" id="sbToggle" title="Toggle">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
  </button>
  <div class="sb-logo">
    <div class="sb-logo-icon">PX</div>
    <div class="sb-logo-text">
      <div class="sb-logo-name">Panel</div>
      <div class="sb-logo-ver">v13.9.4</div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-sec" data-i18n="sec_panel">پنــــل</div>
    <button class="nav-item on" data-page="dash" data-perm="dash">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
      <span class="nav-label" data-i18n="nav_dash">داشبـورد</span>
    </button>
    <button class="nav-item" data-page="configs" data-perm="configs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
      <span class="nav-label" data-i18n="nav_configs">کانفیگ‌هـا</span>
    </button>
    <button class="nav-item" data-page="groups" data-perm="configs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
      <span class="nav-label" data-i18n="nav_groups">گروه‌هـا</span>
    </button>
    <button class="nav-item" data-page="users" data-perm="configs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      <span class="nav-label">کاربران</span>
    </button>
    <button class="nav-item" data-page="create" data-perm="create">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg>
      <span class="nav-label" data-i18n="nav_create">ساخت کانفیـگ</span>
    </button>
    <button class="nav-item" data-page="stats" data-perm="stats">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 5-6"/></svg>
      <span class="nav-label" data-i18n="nav_stats">امـار</span>
    </button>
    <button class="nav-item" data-page="logs" data-perm="logs">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/></svg>
      <span class="nav-label" data-i18n="nav_logs">لاگ فعالیـت</span>
    </button>
    <div class="nav-sec" data-i18n="sec_sys">سیستـم</div>
    <button class="nav-item" data-page="telegram" data-perm="telegram">
      <svg viewBox="0 0 24 24" fill="currentColor" width="18" height="18"><path d="M12 0C5.37 0 0 5.37 0 12s5.37 12 12 12 12-5.37 12-12S18.63 0 12 0zm5.56 8.2-1.86 8.77c-.14.62-.5.77-1.01.48l-2.8-2.06-1.35 1.3c-.15.15-.27.27-.55.27l.2-2.84 5.18-4.68c.22-.2-.05-.31-.35-.12l-6.4 4.03-2.76-.86c-.6-.19-.61-.6.12-.89l10.78-4.16c.5-.18.94.12.78.86z"/></svg>
      <span class="nav-label" data-i18n="nav_telegram">ربات پنل</span>
    </button>
    <button class="nav-item" data-page="news" data-perm="news">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/><path d="M18 14h-8M15 18h-5M10 6h8v4h-8V6Z"/></svg>
      <span class="nav-label" data-i18n="nav_news">اخبـار</span>
    </button>
    <button class="nav-item" data-page="admins" data-perm="admins">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
      <span class="nav-label" data-i18n="nav_admins">ادمین‌هـا</span>
    </button>
    <button class="nav-item" data-page="settings" data-perm="settings">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
      <span class="nav-label" data-i18n="nav_settings">تنظیمـات</span>
    </button>
    <button class="nav-item" data-page="support" data-perm="support">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
      <span class="nav-label" data-i18n="nav_support">پشتیبانـی</span>
    </button>
    <button class="nav-item" data-page="donate">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
      <span class="nav-label" data-i18n="nav_donate">حمایت مالـی</span>
    </button>
  </nav>
  <div class="sb-foot">
    <button type="button" id="themeBtn" onclick="toggleTheme()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
      <span id="themeLabel" data-i18n="theme">تم روشـن</span>
    </button>
    <button type="button" onclick="refreshAll()" title="Stats">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.1-3.4L23 10M1 14l5.4 4.4A9 9 0 0 0 20.5 15"/></svg>
      <span data-i18n="refresh_stats">بروزرسانی امـار</span>
    </button>
    <button type="button" onclick="panelUpdate()" title="Panel" style="background:rgba(16,185,129,.12);border-color:rgba(16,185,129,.35);color:#34d399">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/></svg>
      <span data-i18n="refresh_panel">بروزرسانی پنـل</span>
    </button>
    <a href="/logout" class="btn danger">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16"><path d="M10 5H5v14h5"/><path d="m14 8 4 4-4 4"/><path d="M18 12H9"/></svg>
      <span data-i18n="logout">خروج</span>
    </a>
  </div>
</aside>

<main class="main" id="main">

<section class="page on" id="page-dash">
  <div class="page-head">
    <div>
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <span data-i18n="nav_dash">داشبورد</span>
      </div>
      <div class="page-sub" id="lastUpd" data-i18n="loading">در حال بارگـذاری...</div>
    </div>
  </div>
  <div class="metrics">
    <div class="metric"><div class="metric-label" data-i18n="m_conns">اتصالات فعـال</div><div class="metric-val" id="mConns">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_traffic">ترافیک کـل</div><div class="metric-val" id="mTraffic">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_links">کانفیگ‌هـا</div><div class="metric-val" id="mLinks">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_uptime">آپتایـم سرور</div><div class="metric-val" id="mUptime" style="font-size:17px">—</div></div>
  </div>
  <div class="g2">
    <div class="card action-card" onclick="goPage('create')">
      <div class="card-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg><span data-i18n="quick_create">ساخت کانفیگ</span></div>
      <p style="color:var(--t2);font-size:12px;line-height:1.6" data-i18n="quick_create_desc">ساخت دستی با محدودیت ترافیک، سرعت، تعداد و انقضا</p>
    </div>
    <div class="card action-card purple" onclick="doAutoCreate()">
      <div class="card-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/></svg><span data-i18n="auto_create">ساخت خودکار (پیشنهادی)</span></div>
      <p style="color:var(--t2);font-size:12px;line-height:1.6" data-i18n="auto_create_desc">ساخت سریع با تنظیمات بهینه · لینک VLESS و ساب</p>
    </div>
  </div>
</section>

<section class="page" id="page-configs">
  <div class="page-head">
    <div>
      <div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg><span data-i18n="nav_configs">کانفیگ‌ها</span></div>
      <div class="page-sub" data-i18n="configs_sub">مدیریـت لینک‌هــا · VLESS و سـاب</div>
    </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <input id="cfgSearch" placeholder="جستجو..." oninput="filterConfigs()" style="padding:8px 12px;border-radius:10px;border:1px solid var(--card-b);background:var(--input-bg);color:var(--t1);font-family:inherit;font-size:12px;min-width:140px">

      <button class="btn btn-p btn-sm" onclick="goPage('create')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M12 5v14M5 12h14"/></svg></button>
      <button class="btn btn-sm" onclick="refreshAll()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.1-3.4L23 10"/></svg></button>
    </div>
  </div>
  <div class="card" style="padding:0">
    <div class="table-wrap">
      <div id="bulkBar" style="display:none"></div>
      <table>
        <thead><tr>
          <th style="width:40px;text-align:center;padding:10px 8px">
            <input type="checkbox" id="chkAll" onchange="toggleSelectAll(this.checked);updateBulkBar()" title="انتخاب همه" style="width:16px;height:16px;margin:0;vertical-align:middle;cursor:pointer">
          </th>
          <th style="width:28px;padding:10px 4px"></th>
          <th data-i18n="th_name">نـام</th><th data-i18n="th_proto">پروتکـل</th><th data-i18n="th_status">وضعیت</th>
          <th data-i18n="th_usage">مصـرف</th><th data-i18n="th_ops">عملیـات</th>
        </tr></thead>
        <tbody id="linksTable"><tr><td colspan="7" style="text-align:center;color:var(--t3);padding:32px">...</td></tr></tbody>
      </table>
    </div>
  </div>
</section>

<section class="page" id="page-create">
  <div class="page-head">
    <div><div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M5 12h14"/></svg><span data-i18n="nav_create">ساخت کانفیگ</span></div></div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="card-title" data-i18n="manual_create">ساخت دستی</div>
      <div class="field"><label data-i18n="label_name">نام</label>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="cName" placeholder="auto" style="flex:1">
          <button type="button" class="btn btn-sm" onclick="randomName()" title="Random" style="min-width:44px;height:42px">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>
          </button>
        </div>
      </div>
            <div class="field"><label data-i18n="label_proto">پروتکـل</label><select id="cProto"></select></div>
      <div class="field"><label>گروه</label><select id="cGroup"></select></div>
<div class="form-row">
        <div class="field"><label data-i18n="label_count">تعداد کانفیگ در ساب (۱–۴۰)</label><input id="cCount" type="number" value="1" min="1" max="40"></div>
        <div class="field"><label data-i18n="label_days">انقضـا (روز)</label><input id="cDays" type="number" value="0" min="0"></div>
      </div>
      <div class="form-row">
        <div class="field"><label data-i18n="label_limit">محدودیت حجم</label><input id="cLimit" type="number" value="0" min="0"></div>
        <div class="field"><label data-i18n="label_unit">واحد</label><select id="cUnit"><option>GB</option><option>MB</option><option>KB</option></select></div>
      </div>
      <div class="form-row">
        <div class="field"><label data-i18n="label_ip">محدودیت IP</label><input id="cIp" type="number" value="0" min="0"></div>
        <div class="field"><label data-i18n="label_speed">سرعـت (Mbps)</label><input id="cSpeed" type="number" value="0" min="0"></div>
      </div>
      <button class="btn btn-p" style="width:100%" onclick="doManualCreate()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M12 5v14M5 12h14"/></svg>
        <span data-i18n="btn_create">ساخت</span>
      </button>
    </div>
    <div class="card" style="border-color:rgba(139,92,246,.35)">
      <div class="card-title" data-i18n="auto_create">ساخت خودکار چندپروتکلی</div>
      <p style="color:var(--t2);font-size:13px;line-height:1.75;margin-bottom:14px">از هر پروتکل چند کانفیگ بسازید و همه را در یک ساب با نام دلخواه داشته باشید.</p>
      <div class="field"><label>نام ساب</label><input id="aSubName" placeholder="مثلاً کاربر-۱ یا VIP" maxlength="60"></div>
      <div class="field"><label>پروتکل‌ها و تعداد</label>
        <div id="aProtoList" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;max-height:220px;overflow:auto;padding:4px 0"></div>
      </div>
      <div class="form-row">
        <div class="field"><label data-i18n="label_limit">محدودیت حجم</label><input id="aLimit" type="number" value="0" min="0"></div>
        <div class="field"><label data-i18n="label_unit">واحد</label><select id="aUnit"><option>GB</option><option>MB</option></select></div>
      </div>
      <div class="form-row">
        <div class="field"><label data-i18n="label_days">انقضا (روز)</label><input id="aDays" type="number" value="0" min="0"></div>
        <div class="field"><label data-i18n="label_ip">محدودیت IP</label><input id="aIp" type="number" value="0" min="0"></div>
      </div>
      <div class="field"><label data-i18n="label_speed">سرعت (Mbps)</label><input id="aSpeed" type="number" value="0" min="0"></div>
      <button class="btn btn-p" style="width:100%;background:linear-gradient(135deg,#8b5cf6,#6366f1)" onclick="doMultiAutoCreate()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2"/></svg>
        <span>ساخت ساب چندپروتکلی</span>
      </button>
    </div>
  </div>
</section>


<section class="page" id="page-groups">
  <div class="page-head">
    <div>
      <div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg><span data-i18n="nav_groups">گروه‌ها</span></div>
      <div class="page-sub">ساخـت گـروه و اختصـاص کانفیـگ هـای دستـی و خودکـار</div>
    </div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="card-title">ساخت گروه جدیـد</div>
      <div class="field"><label>نام گروه</label><input id="grpName" placeholder="مثلا اختصاصـی"></div>
      <button class="btn btn-p" style="width:100%" onclick="createGroup()">ساخـت گروه</button>
    </div>
    <div class="card" style="padding:0">
      <div style="padding:16px 18px;border-bottom:1px solid var(--card-b);font-weight:700">لیست گروه‌ها</div>
      <div id="groupsList" style="padding:12px;max-height:480px;overflow:auto">...</div>
    </div>
  </div>
</section>

<section class="page" id="page-users">
  <div class="page-head">
    <div>
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>مدیریت کاربران</span>
      </div>
      <div class="page-sub">ساب‌ها · مصرف · تمدید · فعال/غیرفعال · حذف</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <input id="userSearch" placeholder="جستجوی نام..." oninput="filterUsers()" style="padding:8px 12px;border-radius:10px;border:1px solid var(--card-b);background:var(--input-bg);color:var(--t1);font-family:inherit;font-size:12px;min-width:140px">
      <button class="btn btn-sm" onclick="loadUsers()">بروزرسانی</button>
    </div>
  </div>
  <div class="metrics" style="margin-bottom:14px">
    <div class="metric"><div class="metric-label">تعداد کاربر</div><div class="metric-val" id="uTotal">—</div></div>
    <div class="metric"><div class="metric-label">فعال</div><div class="metric-val" id="uActive">—</div></div>
    <div class="metric"><div class="metric-label">کانفیگ‌ها</div><div class="metric-val" id="uLinks">—</div></div>
    <div class="metric"><div class="metric-label">مصرف کل</div><div class="metric-val" id="uTraffic" style="font-size:15px">—</div></div>
  </div>
  <div class="card" style="padding:0;overflow:hidden">
    <div style="overflow:auto;max-height:min(70vh,720px)">
      <table>
        <thead>
          <tr>
            <th>نام</th>
            <th>مصرف</th>
            <th>کانفیگ</th>
            <th>انقضا</th>
            <th>وضعیت</th>
            <th style="text-align:center">عملیات</th>
          </tr>
        </thead>
        <tbody id="usersBody">
          <tr><td colspan="6" style="text-align:center;color:var(--t3);padding:28px">در حال بارگذاری...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</section>

<section class="page" id="page-stats">
  <div class="page-head">
    <div>
      <div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 5-6"/></svg><span data-i18n="nav_stats">آمار</span></div>
      <div class="page-sub" data-i18n="stats_sub">ترافیـک و اتصـالات · فیلتـر زمانـی</div>
    </div>
    <div class="range-tabs" id="rangeTabs">
      <button class="range-tab" data-r="day" onclick="setRange('day',this)" data-i18n="r_day">روز</button>
      <button class="range-tab" data-r="week" onclick="setRange('week',this)" data-i18n="r_week">هفتـه</button>
      <button class="range-tab on" data-r="month" onclick="setRange('month',this)" data-i18n="r_month">مـاه</button>
      <button class="range-tab" data-r="all" onclick="setRange('all',this)" data-i18n="r_all">کـل</button>
    </div>
  </div>
  <div class="metrics">
    <div class="metric"><div class="metric-label" data-i18n="m_traffic">ترافیـک</div><div class="metric-val" id="sTraffic">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_conns">اتصـالات</div><div class="metric-val" id="sConns">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_links">کانفیـگ فعـال</div><div class="metric-val" id="sActive">—</div></div>
    <div class="metric"><div class="metric-label" data-i18n="m_uptime">آپتایـم</div><div class="metric-val" id="sUptime" style="font-size:16px">—</div></div>
  </div>
  <div class="card"><div class="card-title" data-i18n="panel_info">اطلاعات کل پنل</div><div id="panelInfo" style="font-size:13px;color:var(--t2);line-height:2"></div></div>
  <div class="g2" style="margin-top:14px">
    <div class="card" style="min-height:260px">
      <div class="card-title">نمودار مصرف ساعتی (GB)</div>
      <p style="font-size:11px;color:var(--t3);margin-bottom:10px">ترافیک رله در ساعت‌های اخیر</p>
      <canvas id="chartHourly" width="600" height="220" style="width:100%;max-height:220px;direction:ltr"></canvas>
    </div>
    <div class="card" style="min-height:260px">
      <div class="card-title">پرمصرف‌ترین کانفیگ‌ها (GB)</div>
      <p style="font-size:11px;color:var(--t3);margin-bottom:10px">۱۰ مورد با بیشترین مصرف</p>
      <canvas id="chartTop" width="600" height="220" style="width:100%;max-height:220px;direction:ltr"></canvas>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <div class="card-title">نمودار سهم مصرف کاربران</div>
    <canvas id="chartPie" width="600" height="240" style="width:100%;max-height:260px;direction:ltr"></canvas>
  </div>
</section>

<section class="page" id="page-logs">
  <div class="page-head">
    <div><div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg><span data-i18n="nav_logs">لاگ فعالیت</span></div></div>
    <button class="btn btn-sm" onclick="loadLogs()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.1-3.4L23 10"/></svg></button>
  </div>
  <div class="card" id="logsBox"><div style="text-align:center;color:var(--t3);padding:28px">...</div></div>
</section>

<section class="page" id="page-settings">
  <div class="page-head"><div><div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/></svg><span data-i18n="nav_settings">تنظیمات</span></div></div></div>

  <div class="card" style="border:1px solid rgba(99,102,241,.55);background:linear-gradient(145deg,rgba(99,102,241,.12),rgba(15,15,25,.9));box-shadow:0 0 0 1px rgba(99,102,241,.15),0 12px 40px rgba(99,102,241,.12)">
    <div class="card-title" style="color:#a5b4fc;font-size:15px">🔐 مسیر مخفی پنل (مهم)</div>
    <p style="font-size:12px;color:var(--t2);line-height:1.9;margin-bottom:14px">ورود به پنل فقط از این آدرس ممکن است. این مسیر را ذخیره کنید؛ لینک ساب آن را نشان نمی‌دهد.</p>
    <div class="field"><label style="color:#c7d2fe">مسیر فعلی</label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input id="panelPathShow" readonly value="__PANEL_PATH_DISPLAY__" style="flex:1;min-width:180px;direction:ltr;text-align:left;font-family:ui-monospace,Consolas,monospace;font-weight:800;font-size:14px;color:#c7d2fe;background:rgba(0,0,0,.35);border:1px solid rgba(99,102,241,.4);padding:12px 14px;border-radius:12px">
        <button type="button" class="btn btn-sm btn-p" onclick="copyPanelPath()">کپی مسیر</button>
      </div>
    </div>
    <div class="field"><label style="color:#c7d2fe">لینک ورود کامل</label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input id="panelLoginUrl" readonly value="__PANEL_LOGIN_URL__" style="flex:1;min-width:180px;direction:ltr;text-align:left;font-size:11px;color:#93c5fd;background:rgba(0,0,0,.35);border:1px solid rgba(99,102,241,.4);padding:12px 14px;border-radius:12px">
        <button type="button" class="btn btn-sm btn-p" onclick="copyPanelLogin()">کپی لینک ورود</button>
      </div>
    </div>
    <div id="panelPathEnvNote" style="display:none;font-size:12px;color:#fbbf24;margin:8px 0 12px;line-height:1.7;padding:10px 12px;border-radius:10px;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25)">این مسیر از Environment (PANEL_PATH) آمده و فقط با تغییر env عوض می‌شود.</div>
    <div class="field"><label>تغییر مسیر مخفی</label>
      <input id="panelPathNew" placeholder="مثلا my-secret-panel" maxlength="64" style="direction:ltr;text-align:left">
    </div>
    <button class="btn btn-p" style="width:100%;margin-top:4px" onclick="savePanelPath()">ذخیره مسیر جدید</button>
    <p style="font-size:11px;color:var(--t3);margin-top:12px;line-height:1.75">بعد از ذخیره، با لینک جدید وارد شوید. لینک قبلی دیگر کار نمی‌کند.</p>
  </div>

  <div class="card" style="border-color:rgba(34,211,238,.4)">
    <div class="card-title">پروکسی تلگرام (MTProto)</div>
    <p style="font-size:12px;color:var(--t3);line-height:1.85;margin-bottom:12px">
      پروکسی‌های MTProto را اینجا اضافه کنید. در صورت فعال بودن، لینک‌ها در صفحه ساب کاربران نمایش داده می‌شوند.
    </p>
    <label style="display:flex;align-items:center;gap:8px;font-size:12px;margin-bottom:12px;color:var(--t2)">
      <input type="checkbox" id="mtEnabled"> نمایش لینک‌ها در صفحه ساب کاربران
    </label>
    <div id="mtList" style="display:flex;flex-direction:column;gap:12px;margin-bottom:12px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      <button type="button" class="btn btn-sm" onclick="addMtRow()">+ افزودن پروکسی</button>
      <button type="button" class="btn btn-p" onclick="saveMtproto()">ذخیره همه</button>
    </div>
    <div id="mtLinks" style="font-size:11px;color:#a5f3fc;direction:ltr;text-align:left;line-height:1.85;word-break:break-all;background:rgba(0,0,0,.25);padding:12px;border-radius:12px;border:1px solid rgba(34,211,238,.25)">—</div>
  </div>

    <div class="card-title" data-i18n="theme">تــــم هـا</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn btn-p" onclick="setTheme('dark')" data-i18n="theme_dark">تـم دارک</button>
      <button class="btn" onclick="setTheme('light')" data-i18n="theme_light">تـم روشـن</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title" data-i18n="lang_label">زبـان / Language</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn btn-p" onclick="setLang('fa')">فارسـی</button>
      <button class="btn" onclick="setLang('en')">English</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title" data-i18n="change_pw">تغییر رمز عبـور</div><div class="card-title" data-i18n="change_pw">تغییر رمز عبـور</div>
    <div class="field"><label data-i18n="pw_cur">رمز فعلـی</label><input type="password" id="pwCur"></div>
    <div class="field"><label data-i18n="pw_new">رمـز جدیـد</label><input type="password" id="pwNew"></div>
    <div class="field"><label data-i18n="pw_cf">تکـرار رمـز</label><input type="password" id="pwCf"></div>
    <button class="btn btn-p" onclick="doChangePw()"><span data-i18n="btn_save">ذخیـره</span></button>
  </div>
  
  <div class="card">
    <div class="card-title">امنیت بیشتـر</div>
    <p style="font-size:12px;color:var(--t3);line-height:1.8;margin-bottom:12px">پـس از 5 تـلاش ناموفـق، ایپـی به مدت 30 دقیقه مسدود می‌شود.</p>
    <div id="secStatus" style="font-size:12px;color:var(--t2);margin-bottom:10px">—</div>
    <button class="btn btn-sm" onclick="loadSecurity()">بروزرسانی وضعیـت</button>
    <button class="btn btn-sm btn-d" onclick="unlockAllIps()">رفع مسدودی همـه ایپــی هــا</button>
  </div>
<div class="card">
    <div class="card-title">بــک آپ و بازیابــی</div>
    <p style="font-size:12px;color:var(--t3);line-height:1.8;margin-bottom:14px">در صورت خرابی پنل، بک‌آپ را دانلود کنید و در پنل جدید وارد کنید.</p>
    <div class="g2" style="margin-bottom:12px">
      <button class="btn btn-p" style="width:100%" onclick="downloadBackup('users')">دانلود بک‌آپ کاربران</button>
      <button class="btn btn-p" style="width:100%;background:linear-gradient(135deg,#8b5cf6,#6366f1)" onclick="downloadBackup('bot')">دانلود بک‌آپ ربات</button>
    </div>
    <div class="field">
      <label>وارد کردن بـک‌آپ کاربران</label>
      <input type="file" id="restoreUsersFile" accept="application/json,.json" style="padding:10px">
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
        <button class="btn btn-sm" onclick="restoreUsers('merge')">ادغام بــــا فعلـی</button>
        <button class="btn btn-sm btn-d" onclick="restoreUsers('replace')">جایگزینی کامـل</button>
      </div>
    </div>
    <div class="field" style="margin-top:12px">
      <label>وارد کردن بــک آپ ربـات</label>
      <input type="file" id="restoreBotFile" accept="application/json,.json" style="padding:10px">
      <button class="btn btn-sm" style="margin-top:8px" onclick="restoreBot()">بازیابـی ربـات</button>
    </div>
  </div>
</section>


<section class="page" id="page-news">
  <div class="page-head">
    <div>
      <div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/></svg><span data-i18n="nav_news">اخبار</span></div>
      
    </div>
    <button class="btn btn-sm" onclick="loadNews(true)"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.1-3.4L23 10"/></svg> <span data-i18n="refresh_news">بروزرسانی اطلاعیه</span></button>
  </div>
  <div class="card" id="newsCard">
    <div class="card-title" id="newsTitle">—</div>
    <div id="newsBody" style="white-space:pre-wrap;line-height:1.9;color:var(--t2);font-size:13px">...</div>
    <div id="newsMeta" style="margin-top:14px;font-size:11px;color:var(--t3)"></div>
  </div>
</section>

<section class="page" id="page-admins">
  <div class="page-head">
    <div>
      <div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg><span data-i18n="nav_admins">(نسخـه دمـو) ادمیـن هــا</span></div>
      <div class="page-sub" data-i18n="admins_sub">ساخت اکانت ادمین با دسترسی سفارشـی</div>
    </div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="card-title" data-i18n="admin_create">ساخـت اکانـت ادمیـن</div>
      <div class="field"><label data-i18n="admin_user">نام کاربـری</label><input id="adUser" placeholder="user1" style="direction:ltr;text-align:left"></div>
      <div class="form-row">
        <div class="field"><label data-i18n="admin_pw">رمز عبـور</label><input id="adPw" type="password"></div>
        <div class="field"><label data-i18n="admin_pw2">تکرار رمـز</label><input id="adPw2" type="password"></div>
      </div>
      <div class="form-row">
        <div class="field"><label data-i18n="label_limit">حجـم</label><input id="adLimit" type="number" value="0" min="0"></div>
        <div class="field"><label data-i18n="label_unit">واحـد</label><select id="adUnit"><option>GB</option><option>MB</option></select></div>
      </div>
      <div class="field"><label data-i18n="label_days">مدت اعتبـار (روز)</label><input id="adDays" type="number" value="0" min="0"></div>
      <div class="card-title" style="margin-top:8px" data-i18n="admin_perms">دسترسی‌ها</div>
      <div id="adPerms" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px"></div>
      <button class="btn btn-p" style="width:100%;margin-top:14px" onclick="createAdmin()" data-i18n="admin_btn">ساخت اکانت</button>
    </div>
    <div class="card" style="padding:0">
      <div style="padding:16px 18px;border-bottom:1px solid var(--card-b);font-weight:700" data-i18n="admin_list">لیست ادمین‌ها</div>
      <div id="adminsList" style="padding:12px;max-height:480px;overflow:auto"><div style="color:var(--t3);text-align:center;padding:20px">...</div></div>
    </div>
  </div>
</section>


<section class="page" id="page-donate">
  <div class="page-head" style="justify-content:center">
    <div>
      <div class="page-title" style="justify-content:center">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>
        <span data-i18n="nav_donate">حمایـت مالــــی</span>
      </div>
    </div>
  </div>
  <div style="display:flex;justify-content:center;width:100%">
  <div class="card" style="max-width:560px;width:100%;line-height:2;font-size:14px;color:var(--t2);text-align:center">
    <div style="font-size:16px;font-weight:800;color:var(--t1);margin-bottom:12px">حمایت مالی</div>
    <p>این بخش اختیاری است.</p>
  </div>
  </div>
</section>

<section class="page" id="page-support">
  <div class="page-head"><div><div class="page-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/></svg><span data-i18n="nav_support">پشتیبانی</span></div></div></div>
  <div class="card" style="max-width:560px;margin:0 auto;text-align:center;line-height:2;color:var(--t2)">
    <div style="font-size:15px;font-weight:700;color:var(--t1);margin-bottom:8px">پشتیبانی AGN021G</div>
    <p>برای راهنمایی و پشتیبانی با ما در تماس باشید.</p>
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:16px;text-align:right">
      <a class="btn btn-p" href="https://t.me/AGN021G" target="_blank" rel="noopener" style="justify-content:center">💬 پشتیبانی · @AGN021G</a>
      <a class="btn" href="https://t.me/AGN021GCHAT" target="_blank" rel="noopener" style="justify-content:center">👥 گروه · AGN021GCHAT</a>
      <a class="btn" href="https://t.me/AGN021G1388" target="_blank" rel="noopener" style="justify-content:center">📢 کانال · AGN021G1388</a>
    </div>
    <p style="margin-top:16px;font-size:12px;opacity:.7">ساخته شده توسط AGN021G</p>
  </div>
</section>

<section class="page" id="page-telegram">
  <div class="page-head">
    <div>
      <div class="page-title">
        <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22"><path d="M12 0C5.37 0 0 5.37 0 12s5.37 12 12 12 12-5.37 12-12S18.63 0 12 0zm5.56 8.2-1.86 8.77c-.14.62-.5.77-1.01.48l-2.8-2.06-1.35 1.3c-.15.15-.27.27-.55.27l.2-2.84 5.18-4.68c.22-.2-.05-.31-.35-.12l-6.4 4.03-2.76-.86c-.6-.19-.61-.6.12-.89l10.78-4.16c.5-.18.94.12.78.86z"/></svg>
        <span data-i18n="nav_telegram">ربات پنل</span>
      </div>
      <div class="page-sub" data-i18n="tg_sub">توکن ربات و آیدی عددی ادمین · فعال‌سازی خودکار و وب‌هوک</div>
    </div>
  </div>
  <div class="card">
    <div class="card-title" data-i18n="tg_config">پیکربندی ربات</div>
    <div class="field"><label data-i18n="tg_token">توکن ربات (BotFather)</label><input id="tgToken" placeholder="123456:ABC-DEF..." autocomplete="off"></div>
    <div class="field"><label data-i18n="tg_admin">آیدی عددی ادمین</label><input id="tgAdmin" placeholder="123456789" inputmode="numeric"></div>
    <div class="field" style="display:flex;align-items:center;gap:10px">
      <label class="switch"><input type="checkbox" id="tgWebhook" checked><span class="slider"></span></label>
      <span data-i18n="tg_webhook" style="font-size:13px;color:var(--t2)">فعال‌سازی Webhook (پیشنهادی روی Railway)</span>
    </div>
    <div id="tgStatus" style="font-size:12px;color:var(--t3);margin:10px 0"></div>
    <button class="btn btn-p" style="width:100%" onclick="saveTelegram()">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      <span data-i18n="tg_activate">ذخیره و فعال‌سازی ربات</span>
    </button>
  </div>
  <div class="card">
    <div class="card-title" data-i18n="tg_help">راهنما</div>
    <ol style="color:var(--t2);font-size:13px;line-height:2;padding-right:18px">
      <li data-i18n="tg_h1">از @BotFather یک ربات بساز و توکن را کپی کن</li>
      <li data-i18n="tg_h2">آیدی عددی خودت را از @userinfobot بگیر</li>
      <li data-i18n="tg_h3">ذخیره کن — وب‌هوک خودکار روی دامنه Railway ست می‌شود</li>
    </ol>
  </div>
</section>

</main>

<!-- Result modal after create -->
<div class="modal-bg" id="resultModal">
  <div class="modal">
    <div class="modal-title" data-i18n="created_title">کانفیگ ساخته شد</div>
    <div class="field"><label>VLESS</label><div class="link-box" id="resVless">—</div>
      <button class="btn btn-p btn-sm" style="width:100%" onclick="copyText(document.getElementById('resVless').textContent)">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        <span data-i18n="copy_vless">کپی VLESS</span>
      </button>
    </div>
    <div class="field" style="margin-top:14px"><label data-i18n="sub_label">سابسکریپشن</label><div class="link-box" id="resSub">—</div>
      <button class="btn btn-sm" style="width:100%" onclick="copyText(document.getElementById('resSub').textContent)">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg>
        <span data-i18n="copy_sub">کپی ساب</span>
      </button>
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="closeResult()">OK</button>
    </div>
  </div>
</div>

<div class="modal-bg" id="panelModal">
  <div class="modal">
    <div class="modal-title" id="panelModalTitle">...</div>
    <div id="panelModalBody" style="color:var(--t2);font-size:13px;line-height:1.8"></div>
    <div class="modal-actions">
      <button class="btn" onclick="document.getElementById('panelModal').classList.remove('open')">OK</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const I18N={
fa:{sec_panel:'پنل',sec_sys:'سیستم',nav_dash:'داشبورد',nav_configs:'کانفیگ‌ها',nav_groups:'گروه‌ها',nav_create:'ساخت کانفیگ',nav_stats:'آمار',nav_logs:'لاگ فعالیت',nav_settings:'تنظیمات',nav_support:'پشتیبانی',nav_donate:'حمایت مالی',nav_news:'اخبار',nav_admins:'ادمین‌ها',refresh_news:'بروزرسانی اطلاعیه',admins_sub:'ساخت اکانت ادمین با دسترسی سفارشی',admin_create:'ساخت اکانت ادمین',admin_user:'نام کاربری',admin_pw:'رمز عبور',admin_pw2:'تکرار رمز',admin_perms:'دسترسی‌ها',admin_btn:'ساخت اکانت',admin_list:'لیست ادمین‌ها',refresh:'بروزرسانی',refresh_stats:'بروزرسانی آمار',refresh_panel:'بروزرسانی پنل',nav_telegram:'ربات تلگرام',tg_sub:'توکن ربات و آیدی عددی ادمین · فعال‌سازی خودکار و وب‌هوک',tg_config:'پیکربندی ربات',tg_token:'توکن ربات (BotFather)',tg_admin:'آیدی عددی ادمین',tg_webhook:'فعال‌سازی Webhook (پیشنهادی روی Railway)',tg_activate:'ذخیره و فعال‌سازی ربات',tg_help:'راهنما',tg_h1:'از @BotFather یک ربات بساز و توکن را کپی کن',tg_h2:'آیدی عددی خودت را از @userinfobot بگیر',tg_h3:'ذخیره کن — وب‌هوک خودکار روی دامنه Railway ست می‌شود',logout:'خروج',loading:'در حال بارگذاری...',m_conns:'اتصالات فعال',m_traffic:'ترافیک کل',m_links:'کانفیگ‌ها',m_uptime:'آپتایم سرور',quick_create:'ساخت کانفیگ',quick_create_desc:'ساخت دستی با محدودیت ترافیک، سرعت، تعداد و انقضا',auto_create:'ساخت خودکار (پیشنهادی)',auto_create_desc:'ساخت سریع با تنظیمات بهینه · لینک VLESS و ساب',configs_sub:'مدیریت لینک‌ها · VLESS و ساب',th_name:'نام',th_proto:'پروتکل',th_status:'وضعیت',th_usage:'مصرف',th_ops:'عملیات',manual_create:'ساخت دستی',label_name:'نام',label_proto:'پروتکل',label_count:'تعداد کانفیگ در ساب (۱–۴۰)',label_limit:'محدودیت حجم',label_unit:'واحد',label_days:'انقضا (روز)',label_ip:'محدودیت IP',label_speed:'سرعت (Mbps)',btn_create:'ساخت',btn_auto:'ساخت خودکار',auto_desc:'با یک کلیک کانفیگ بهینه ساخته می‌شود. بعد از ساخت لینک VLESS و ساب در اختیار شماست.',stats_sub:'ترافیک و اتصالات · فیلتر زمانی',r_day:'روز',r_week:'هفته',r_month:'ماه',r_all:'کل',panel_info:'اطلاعات کل پنل',lang_label:'زبان',change_pw:'تغییر رمز عبور',pw_cur:'رمز فعلی',pw_new:'رمز جدید',pw_cf:'تکرار رمز',btn_save:'ذخیره',github:'گیت‌هاب',telegram:'تلگرام',channel:'کانال پشتیبان',theme:'تم',theme_dark:'تم تیره',theme_light:'تم روشن',created_title:'کانفیگ ساخته شد',copy_vless:'کپی VLESS',copy_sub:'کپی ساب',sub_label:'سابسکریپشن'},
en:{sec_panel:'PANEL',sec_sys:'SYSTEM',nav_dash:'Dashboard',nav_configs:'Configs',nav_groups:'Groups',nav_create:'Create Config',nav_stats:'Statistics',nav_logs:'Activity Log',nav_settings:'Settings',nav_support:'Support',nav_donate:'Donate',nav_news:'News',nav_admins:'Admins',refresh_news:'Refresh news',admins_sub:'Create admin accounts with custom access',admin_create:'Create admin account',admin_user:'Username',admin_pw:'Password',admin_pw2:'Confirm password',admin_perms:'Permissions',admin_btn:'Create account',admin_list:'Admin list',refresh:'Refresh',refresh_stats:'Refresh stats',refresh_panel:'Update panel',nav_telegram:'Telegram bot',tg_sub:'Bot token and numeric admin ID · auto activate and webhook',tg_config:'Bot configuration',tg_token:'Bot token (BotFather)',tg_admin:'Admin numeric ID',tg_webhook:'Enable Webhook (recommended on Railway)',tg_activate:'Save and activate bot',tg_help:'Guide',tg_h1:'Create a bot with @BotFather and copy the token',tg_h2:'Get your numeric ID from @userinfobot',tg_h3:'Save — webhook is set automatically on Railway domain',logout:'Logout',loading:'Loading...',m_conns:'Active connections',m_traffic:'Total traffic',m_links:'Configs',m_uptime:'Server uptime',quick_create:'Create Config',quick_create_desc:'Manual create with traffic, speed, count and expiry',auto_create:'Auto Create (Suggested)',auto_create_desc:'Quick optimal create · VLESS and Sub links',configs_sub:'Manage links · VLESS and Sub',th_name:'Name',th_proto:'Protocol',th_status:'Status',th_usage:'Usage',th_ops:'Actions',manual_create:'Manual create',label_name:'Name',label_proto:'Protocol',label_count:'Configs in sub (1–40)',label_limit:'Traffic limit',label_unit:'Unit',label_days:'Expiry (days)',label_ip:'IP limit',label_speed:'Speed (Mbps)',btn_create:'Create',btn_auto:'Auto create',auto_desc:'One click creates an optimal config. VLESS and Sub links will be shown.',stats_sub:'Traffic and connections · time filter',r_day:'Day',r_week:'Week',r_month:'Month',r_all:'All',panel_info:'Panel overview',lang_label:'Language',change_pw:'Change password',pw_cur:'Current password',pw_new:'New password',pw_cf:'Confirm password',btn_save:'Save',github:'GitHub',telegram:'Telegram',channel:'Support channel',theme:'Theme',theme_dark:'Dark theme',theme_light:'Light theme',created_title:'Config created',copy_vless:'Copy VLESS',copy_sub:'Copy Sub',sub_label:'Subscription'}
};
let lang=localStorage.getItem('px_lang')||'fa';
let statRange='month';
function t(k){return (I18N[lang]||I18N.fa)[k]||k}
function applyLang(){
  document.getElementById('htmlRoot').lang=lang;
  document.getElementById('htmlRoot').dir=lang==='fa'?'rtl':'ltr';
  document.body.classList.toggle('en',lang==='en');
  document.querySelectorAll('[data-i18n]').forEach(el=>{const k=el.getAttribute('data-i18n');if(I18N[lang][k])el.textContent=I18N[lang][k]});
  const tl=document.getElementById('themeLabel');
  if(tl) tl.textContent=document.documentElement.classList.contains('light')?t('theme_dark'):t('theme_light');
}
function setLang(l){lang=l;localStorage.setItem('px_lang',l);applyLang();toast(l==='fa'?'زبان فارسی':'English')}

function setTheme(mode){
  if(mode==='light') document.documentElement.classList.add('light');
  else document.documentElement.classList.remove('light');
  localStorage.setItem('px_theme',mode);
  applyLang();
}
function toggleTheme(){
  const isLight=document.documentElement.classList.contains('light');
  setTheme(isLight?'dark':'light');
}
(function(){const th=localStorage.getItem('px_theme')||'dark';setTheme(th)})();

const sb=document.getElementById('sidebar'),main=document.getElementById('main');
document.getElementById('sbToggle').onclick=()=>{
  sb.classList.toggle('collapsed');
  main.classList.toggle('expanded',sb.classList.contains('collapsed'));
  localStorage.setItem('sb_c',sb.classList.contains('collapsed')?'1':'0');
};
if(localStorage.getItem('sb_c')==='1'){sb.classList.add('collapsed');main.classList.add('expanded')}
document.getElementById('mobMenuBtn').onclick=()=>{sb.classList.add('open');document.getElementById('overlay').classList.add('show')};
document.getElementById('overlay').onclick=()=>{sb.classList.remove('open');document.getElementById('overlay').classList.remove('show')};

function goPage(name){
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('on',n.dataset.page===name));
  document.querySelectorAll('.page').forEach(p=>p.classList.toggle('on',p.id==='page-'+name));
  sb.classList.remove('open');document.getElementById('overlay').classList.remove('show');
  window.scrollTo({top:0,behavior:'smooth'});
  if(name==='logs')loadLogs();
  if(name==='configs'||name==='dash'||name==='stats'){refreshAll();if(name==='stats')setTimeout(loadUsageCharts,50);}
}
document.querySelectorAll('.nav-item').forEach(el=>el.addEventListener('click',()=>goPage(el.dataset.page)));

function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  clearTimeout(window.__tt);window.__tt=setTimeout(()=>el.classList.remove('show'),2200);
}
const PB=(typeof window!=='undefined'&&window.PANEL_BASE)?window.PANEL_BASE:'';
function panelUrl(path){if(!path)return PB||'/';if(path.startsWith('http'))return path;const p=path.startsWith('/')?path:'/'+path;return (PB||'')+p}
async function api(url,opts={}){
  try{
    const r=await fetch(panelUrl(url),{cache:'no-store',credentials:'same-origin',...opts});
    if(r.status===401){location.href=panelUrl('/login');return null}
    let data=null;try{data=await r.json()}catch{data={ok:false}}
    if(!r.ok){toast(data.detail||data.error||'Error');return null}
    return data;
  }catch(e){toast(lang==='fa'?'ارتباط برقرار نشد':'Connection failed');return null}
}
function fmtB(b){b=Number(b)||0;const gb=b/(1024**3);if(b===0)return '0 GB';if(gb<0.01)return gb.toFixed(4)+' GB';if(gb<10)return gb.toFixed(3)+' GB';return gb.toFixed(2)+' GB'}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}


function _cssVar(n,fb){try{return getComputedStyle(document.documentElement).getPropertyValue(n).trim()||fb}catch(e){return fb}}
function drawBarChart(canvas,labels,values,opts){
  if(!canvas)return;
  const dpr=window.devicePixelRatio||1;
  const cssW=canvas.clientWidth||600, cssH=canvas.clientHeight||220;
  canvas.width=Math.floor(cssW*dpr); canvas.height=Math.floor(cssH*dpr);
  const ctx=canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const W=cssW,H=cssH, pad={t:16,r:12,b:36,l:44};
  ctx.clearRect(0,0,W,H);
  const maxV=Math.max(0.001, ...values.map(v=>Number(v)||0));
  const n=Math.max(1,values.length);
  const gw=(W-pad.l-pad.r)/n;
  const barW=Math.max(4, Math.min(28, gw*0.62));
  // grid
  ctx.strokeStyle='rgba(148,163,184,.15)'; ctx.lineWidth=1;
  for(let i=0;i<=4;i++){
    const y=pad.t+(H-pad.t-pad.b)*(i/4);
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(W-pad.r,y); ctx.stroke();
    const val=maxV*(1-i/4);
    ctx.fillStyle='rgba(148,163,184,.65)'; ctx.font='10px Vazirmatn,sans-serif'; ctx.textAlign='right';
    ctx.fillText(val>=10?val.toFixed(1):val.toFixed(2), pad.l-6, y+3);
  }
  const grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  grad.addColorStop(0,'#60a5fa'); grad.addColorStop(1,'#8b5cf6');
  values.forEach((v,i)=>{
    v=Number(v)||0;
    const h=((H-pad.t-pad.b)*v)/maxV;
    const x=pad.l+i*gw+(gw-barW)/2;
    const y=H-pad.b-h;
    ctx.fillStyle=grad;
    const r=6;
    ctx.beginPath();
    ctx.moveTo(x,y+r); ctx.arcTo(x,y,x+barW,y,r); ctx.arcTo(x+barW,y,x+barW,y+h,r);
    ctx.lineTo(x+barW,H-pad.b); ctx.lineTo(x,H-pad.b); ctx.closePath(); ctx.fill();
    const lab=labels[i]||'';
    ctx.fillStyle='rgba(148,163,184,.8)'; ctx.font='10px Vazirmatn,sans-serif'; ctx.textAlign='center';
    ctx.fillText(String(lab).slice(0,8), x+barW/2, H-pad.b+14);
  });
}
function drawPieChart(canvas,labels,values){
  if(!canvas)return;
  const dpr=window.devicePixelRatio||1;
  const cssW=canvas.clientWidth||600, cssH=canvas.clientHeight||240;
  canvas.width=Math.floor(cssW*dpr); canvas.height=Math.floor(cssH*dpr);
  const ctx=canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const W=cssW,H=cssH; ctx.clearRect(0,0,W,H);
  const total=values.reduce((a,b)=>a+(Number(b)||0),0)||1;
  const cx=W*0.32, cy=H/2, R=Math.min(W,H)*0.32;
  const colors=['#3b82f6','#8b5cf6','#22d3ee','#f472b6','#f59e0b','#34d399','#f87171','#a78bfa','#38bdf8','#fb7185'];
  let ang=-Math.PI/2;
  values.forEach((v,i)=>{
    const slice=2*Math.PI*((Number(v)||0)/total);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,R,ang,ang+slice); ctx.closePath();
    ctx.fillStyle=colors[i%colors.length]; ctx.fill();
    ang+=slice;
  });
  // legend
  ctx.font='11px Vazirmatn,sans-serif';
  values.forEach((v,i)=>{
    const y=24+i*18;
    if(y>H-8)return;
    ctx.fillStyle=colors[i%colors.length];
    ctx.fillRect(W*0.58, y-8, 10, 10);
    ctx.fillStyle='rgba(248,250,252,.75)';
    const pct=((Number(v)||0)/total*100).toFixed(1);
    const name=String(labels[i]||'').slice(0,16);
    ctx.textAlign='left';
    ctx.fillText(name+' · '+fmtB(v)+' ('+pct+'%)', W*0.58+16, y);
  });
  if(total<=0 || values.every(v=>!Number(v))){
    ctx.fillStyle='rgba(148,163,184,.7)'; ctx.font='13px Vazirmatn,sans-serif'; ctx.textAlign='center';
    ctx.fillText(lang==='fa'?'هنوز مصرفی ثبت نشده':'No usage yet', W/2, H/2);
  }
}
async function loadUsageCharts(){
  try{
    const st=await api('/stats');
    const hourly=(st&&st.hourly)||{};
    // sort hours
    const keys=Object.keys(hourly).sort();
    const hLabels=keys.length?keys:Array.from({length:12},(_,i)=>String(i).padStart(2,'0')+':00');
    const hVals=keys.length?keys.map(k=>(Number(hourly[k])||0)/(1024**3)):hLabels.map(()=>0);
    drawBarChart(document.getElementById('chartHourly'), hLabels, hVals);

    const top=(st&&st.top_links)||[];
    const tLabels=top.map(x=>x.label||x.id||'?');
    const tVals=top.map(x=>(Number(x.used_bytes)||0)/(1024**3));
    if(!tLabels.length){ tLabels.push('—'); tVals.push(0); }
    drawBarChart(document.getElementById('chartTop'), tLabels, tVals);
    drawPieChart(document.getElementById('chartPie'), tLabels, top.map(x=>Number(x.used_bytes)||0));
  }catch(e){}
}

async function refreshAll(){
  if(typeof loadGroups==='function') try{await loadGroups()}catch(e){}
  const links=await api('/api/links');
  if(!links)return;
  const arr=Array.isArray(links.links)?links.links:(Array.isArray(links)?links:[]);
  document.getElementById('mLinks').textContent=arr.length;
  let active=0,used=0;
  arr.forEach(l=>{if(l.active!==false)active++;used+=Number(l.used_bytes||0)});
  document.getElementById('mTraffic').textContent=fmtB(used);
  document.getElementById('sTraffic').textContent=fmtB(used);
  document.getElementById('sActive').textContent=active;
  document.getElementById('lastUpd').textContent=(lang==='fa'?'بروزرسانی: ':'Updated: ')+new Date().toLocaleTimeString(lang==='fa'?'fa-IR':'en-US');
  try{
    const c=await api('/api/connections');
    const cnt=(c&&c.connections)?c.connections.length:((c&&typeof c.count==='number')?c.count:0);
    document.getElementById('mConns').textContent=cnt;
    document.getElementById('sConns').textContent=cnt;
  }catch(e){}
  try{
    const h=await fetch('/health',{cache:'no-store'}).then(r=>r.json());
    if(h&&h.uptime){
      document.getElementById('mUptime').textContent=h.uptime;
      const su=document.getElementById('sUptime');if(su)su.textContent=h.uptime;
    }
  }catch(e){}
    __allLinks=arr;
  softUpdateLinks(arr);
  document.getElementById('panelInfo').innerHTML=lang==='fa'
    ?`کل کانفیگ: <b>${arr.length}</b> · فعال: <b>${active}</b> · مصرف: <b>${fmtB(used)}</b> · بازه: <b>${statRange}</b>`
    :`Total: <b>${arr.length}</b> · Active: <b>${active}</b> · Usage: <b>${fmtB(used)}</b> · Range: <b>${statRange}</b>`;
  try{loadUsageCharts()}catch(e){}
}


function linkBadgeClass(l){
  const conn=Number(l.connected_ips||0);
  const used=Number(l.used_bytes||0), lim=Number(l.limit_bytes||0);
  let usagePct=lim>0?(used/lim)*100:0;
  let expWarn=false, expDead=false;
  if(l.expires_at){try{const ms=new Date(l.expires_at)-Date.now();if(ms<=0)expDead=true;else if(ms<3*864e5)expWarn=true}catch(e){}}
  if(expDead||usagePct>=90) return 'conn-badge red';
  if(expWarn||usagePct>=70) return 'conn-badge orange';
  if(conn>0) return 'conn-badge green';
  return 'conn-badge gray';
}
function softUpdateLinks(arr){
  const tb=document.getElementById('linksTable');
  if(!tb) return;
  const rows=[...tb.querySelectorAll('tr[data-uid]')];
  const existing=rows.map(r=>r.getAttribute('data-uid'));
  const incoming=arr.map(l=>String(l.uuid||l.id||''));
  const same = existing.length===incoming.length && existing.every((id,i)=>id===incoming[i]);
  // اگر در حال درگ یا انتخاب هستیم، فقط سلول‌ها را آپدیت کن
  const selecting = document.querySelectorAll('.cfg-chk:checked').length>0;
  const dragging = !!__dragUid;
  if(!same || existing.length===0){
    if(dragging || selecting){
      // فقط آمار ردیف‌های موجود را آپدیت کن، ساختار را نشکن
      window.__linksMap = window.__linksMap || {};
      arr.forEach(l=>{
        const uid=String(l.uuid||l.id||'');
        window.__linksMap[uid]=l;
        const tr=tb.querySelector(`tr[data-uid="${uid}"]`);
        if(!tr) return;
        patchLinkRow(tr, l);
      });
      return;
    }
    renderLinks(arr);
    return;
  }
  window.__linksMap = window.__linksMap || {};
  arr.forEach(l=>{
    const uid=String(l.uuid||l.id||'');
    window.__linksMap[uid]=l;
    const tr=tb.querySelector(`tr[data-uid="${uid}"]`);
    if(tr) patchLinkRow(tr, l);
  });
}
function patchLinkRow(tr, l){
  const conn=Number(l.connected_ips||0);
  const badge=tr.querySelector('.conn-badge');
  if(badge){ badge.textContent=String(conn); badge.className=linkBadgeClass(l); }
  const usageCell=tr.querySelector('[data-usage]');
  if(usageCell){
    usageCell.textContent = fmtB(l.used_bytes) + (l.limit_bytes?(' / '+fmtB(l.limit_bytes)):'');
  }
  // وضعیت سوئیچ را اگر کاربر همین الان عوض نکرده دست نزن — فقط اگر API فرق دارد و فوکوس نیست
  const sw=tr.querySelector('.switch input[type=checkbox]');
  if(sw && document.activeElement!==sw){
    const on=l.active!==false&&!l.expired;
    if(sw.checked!==on) sw.checked=on;
  }
}
function renderLinks(arr){
  const tb=document.getElementById('linksTable');
  if(!arr.length){tb.innerHTML=`<tr><td colspan="7" style="text-align:center;color:var(--t3);padding:28px">${lang==='fa'?'کانفیگی نیست':'No configs'}</td></tr>`;updateBulkBar();return}
  window.__linksMap={};
  const catMap=window.__catMap||{};
  // preserve checked state
  const prevChecked=new Set([...document.querySelectorAll('.cfg-chk:checked')].map(c=>c.value));
  tb.innerHTML=arr.map(l=>{
    const uid=l.uuid||l.id||'';
    window.__linksMap[uid]=l;
    const name=l.label||l.name||String(uid).slice(0,8);
    const proto=l.protocol||'vless-ws';
    const on=l.active!==false&&!l.expired;
    const conn=Number(l.connected_ips||0);
    const gname=catMap[String(l.category_id||'')]||'';
    const chk=prevChecked.has(uid)?'checked':'';
    return `<tr draggable="true" data-uid="${esc(uid)}" ondragstart="cfgDragStart(event)" ondragover="cfgDragOver(event)" ondrop="cfgDrop(event)" ondragend="cfgDragEnd(event)">
      <td style="text-align:center;padding:10px 8px;vertical-align:middle"><input type="checkbox" class="cfg-chk" value="${esc(uid)}" ${chk} onchange="updateBulkBar()" style="width:16px;height:16px;margin:0;vertical-align:middle;cursor:pointer"></td>
      <td style="cursor:grab;color:var(--t3);user-select:none" title="کشیدن">⋮⋮</td>
      <td>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <b>${esc(name)}</b>
          <span class="${linkBadgeClass(l)}" title="${lang==='fa'?'متصل الان':'Online now'}">${conn}</span>
          ${gname?`<span style="font-size:10px;padding:2px 7px;border-radius:8px;background:var(--hover);color:var(--t3)">${esc(gname)}</span>`:''}
        </div>
      </td>
      <td style="color:var(--t3);font-size:11px">${esc(proto)}</td>
      <td><label class="switch"><input type="checkbox" ${on?'checked':''} onchange="toggleLink('${esc(uid)}',this.checked)"><span class="slider"></span></label></td>
      <td data-usage>${fmtB(l.used_bytes)}${l.limit_bytes?(' / '+fmtB(l.limit_bytes)):''}</td>
      <td class="ops">
        <button class="btn btn-sm" onclick="copyLinkById('${esc(uid)}')" title="VLESS"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="btn btn-sm" onclick="copySubById('${esc(uid)}')" title="Sub"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 11a9 9 0 0 1 9 9M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg></button>
        <a class="btn btn-sm" href="/info/${esc(uid)}" target="_blank" title="INFO" style="text-decoration:none"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg></a>
        <button class="btn btn-sm" onclick="resetUsage('${esc(uid)}')" title="Reset"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg></button>
        <button class="btn btn-sm btn-d" onclick="deleteLink('${esc(uid)}')"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg></button>
      </td>
    </tr>`;
  }).join('');
  updateBulkBar();
}
function getLinkUrl(l){if(!l)return '';return l.vless_full||l.vless||l.vless_link||l.link||''}
function getSubUrl(l){if(!l)return '';return l.sub||l.sub_url||l.info||''}
async function copyText(text){
  text=String(text||'').trim();
  if(!text||text==='—'){toast(lang==='fa'?'لینکی نیست':'Nothing to copy');return}
  try{
    if(navigator.clipboard&&window.isSecureContext) await navigator.clipboard.writeText(text);
    else{const ta=document.createElement('textarea');ta.value=text;ta.style.cssText='position:fixed;left:-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta)}
    toast(lang==='fa'?'کپی شد':'Copied');
  }catch(e){toast(lang==='fa'?'کپی نشد':'Copy failed')}
}
async function copyLinkById(uid){await copyText(getLinkUrl((window.__linksMap||{})[uid]))}
async function copySubById(uid){await copyText(getSubUrl((window.__linksMap||{})[uid]))}
async function toggleLink(uid,state){
  // optimistic UI — رنگ بلافاصله عوض می‌شود
  if(window.__linksMap && window.__linksMap[uid]){
    window.__linksMap[uid].active = !!state;
    if(window.__linksMap[uid].expired && state) window.__linksMap[uid].expired = false;
  }
  if(typeof __allLinks !== 'undefined' && Array.isArray(__allLinks)){
    const item = __allLinks.find(x => (x.uuid||x.id)===uid);
    if(item) item.active = !!state;
  }
  const r=await api('/api/links/'+uid,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!!state})});
  if(r===null){
    // rollback
    if(window.__linksMap && window.__linksMap[uid]) window.__linksMap[uid].active = !state;
    refreshAll();
    return;
  }
  toast(state?(lang==='fa'?'فعال شد':'Enabled'):(lang==='fa'?'غیرفعال شد':'Disabled'));
}
async function deleteLink(uid){
  if(!confirm(lang==='fa'?'حذف شود؟':'Delete?'))return;
  const r=await api('/api/links/'+uid,{method:'DELETE'});
  if(r!==null){toast(lang==='fa'?'حذف شد':'Deleted');refreshAll()}
}
function showResult(data){
  if(!data)return;
  document.getElementById('resVless').textContent=getLinkUrl(data)||'—';
  document.getElementById('resSub').textContent=getSubUrl(data)||'—';
  document.getElementById('resultModal').classList.add('open');
}
function closeResult(){document.getElementById('resultModal').classList.remove('open')}
document.getElementById('resultModal').addEventListener('click',e=>{if(e.target.id==='resultModal')closeResult()});

async function doAutoCreate(){
  // keep for dashboard quick button — single protocol balanced
  toast(lang==='fa'?'در حال ساخت...':'Creating...');
  let r=await api('/api/links/auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config_count:1,protocol:'vless-ws'})});
  if(r){showResult(r);refreshAll()}
}
function fillMultiProtoList(){
  const box=document.getElementById('aProtoList');
  if(!box)return;
  const preferred=['vless-ws','xhttp-packet-up','xhttp-stream-up','xhttp-stream-one','vmess-ws','trojan-ws','shadowsocks','socks5','http','hysteria2','tuic','wireguard','highspeed-demo','gaming-lite-demo'];
  const labels={
    'vless-ws':'VLESS WebSocket','xhttp-packet-up':'XHTTP Packet Up','xhttp-stream-up':'XHTTP Stream Up','xhttp-stream-one':'XHTTP Stream One',
    'vmess-ws':'VMess WebSocket','trojan-ws':'Trojan WebSocket','shadowsocks':'Shadowsocks','socks5':'SOCKS5','http':'HTTP Proxy',
    'hysteria2':'Hysteria 2','tuic':'TUIC','wireguard':'WireGuard',
    'highspeed-demo':'HighSpeed (دمو)','gaming-lite-demo':'Gaming Lite (دمو)'
  };
  // all protocols from API + preferred (no filter out demos)
  const fromApi=window._PROTOCOLS||[];
  const list=[...new Set([...preferred, ...fromApi])];
  box.innerHTML=list.map(pid=>{
    const lab=labels[pid]||pid;
    const def=['vless-ws','xhttp-stream-up'].includes(pid)?1:0;
    return `<label style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:12px;background:var(--bg3);border:1px solid var(--card-b);font-size:12px;cursor:pointer">
      <input type="checkbox" class="aProtoChk" data-proto="${pid}" ${def?'checked':''} style="width:15px;height:15px;accent-color:var(--accent)">
      <span style="flex:1;font-weight:600">${lab}</span>
      <input type="number" class="aProtoCnt" data-proto="${pid}" value="${def||1}" min="0" max="20" style="width:52px;padding:4px 6px;border-radius:8px;border:1px solid var(--card-b);background:var(--input-bg);color:var(--t1);font-size:12px;text-align:center" onclick="event.stopPropagation()">
    </label>`;
  }).join('');
}
async function doMultiAutoCreate(){
  toast(lang==='fa'?'در حال ساخت ساب...':'Creating sub...');
  const protocols={};
  document.querySelectorAll('.aProtoChk').forEach(chk=>{
    if(!chk.checked)return;
    const pid=chk.dataset.proto;
    const cntEl=document.querySelector(`.aProtoCnt[data-proto="${pid}"]`);
    const n=Math.max(0,Math.min(20,Number(cntEl&&cntEl.value)||0));
    if(n>0)protocols[pid]=n;
  });
  if(!Object.keys(protocols).length){toast(lang==='fa'?'حداقل یک پروتکل انتخاب کنید':'Select at least one protocol');return}
  const body={
    sub_name:(document.getElementById('aSubName')?.value||'').trim()||undefined,
    protocols,
    limit_value:Number(document.getElementById('aLimit')?.value)||0,
    limit_unit:document.getElementById('aUnit')?.value||'GB',
    expires_days:Number(document.getElementById('aDays')?.value)||0,
    ip_limit:Number(document.getElementById('aIp')?.value)||0,
    speed_limit_value:Number(document.getElementById('aSpeed')?.value)||0,
    profile:'balanced'
  };
  const r=await api('/api/links/multi-auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r&&r.ok){
    showMultiResult(r);
    refreshAll();
    if(typeof loadGroups==='function')loadGroups();
  }
}
function showMultiResult(r){
  const m=document.getElementById('panelModal');
  const t=document.getElementById('panelModalTitle');
  const b=document.getElementById('panelModalBody');
  if(!m){toast('ساخته شد: '+(r.sub||''));return}
  t.textContent=lang==='fa'?'ساب چندپروتکلی ساخته شد':'Multi-protocol sub created';
  const protoLines=Object.entries(r.protocols||{}).map(([k,v])=>`${k}: ${v}`).join(' · ');
  const one=r.sub||r.page||'';
  b.innerHTML=`<div style="line-height:2;font-size:13px">
    <div><b>نام:</b> ${esc(r.sub_name||'')}</div>
    <div><b>تعداد کانفیگ:</b> ${r.count||0}</div>
    <div style="color:var(--t3);font-size:12px">${esc(protoLines)}</div>
    <p style="margin-top:10px;font-size:12px;color:var(--t2)">یک لینک برای v2ray و مشاهده مصرف در مرورگر:</p>
    <div style="margin-top:8px;padding:12px;border-radius:12px;background:var(--bg3);border:1px solid var(--card-b);direction:ltr;text-align:left;word-break:break-all;font-size:11px;color:#93c5fd" id="multiSubUrl">${esc(one)}</div>
    <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
      <button class="btn btn-p" onclick="navigator.clipboard.writeText(document.getElementById('multiSubUrl').textContent);toast(lang==='fa'?'کپی شد':'Copied')">کپی لینک</button>
      <a class="btn" href="${esc(one)}" target="_blank" rel="noopener">باز کردن</a>
    </div>
  </div>`;
  m.classList.add('open');
}
async function doManualCreate(){
  const body={
    label:document.getElementById('cName').value||undefined,
    protocol:document.getElementById('cProto')?.value||undefined,
    category_id:document.getElementById('cGroup')?.value||'0',
    config_count:Math.max(1,Math.min(40,Number(document.getElementById('cCount').value)||1)),
    limit_value:Number(document.getElementById('cLimit').value)||0,
    limit_unit:document.getElementById('cUnit').value||'GB',
    expires_days:Number(document.getElementById('cDays').value)||0,
    ip_limit:Number(document.getElementById('cIp').value)||0,
    speed_limit_value:Number(document.getElementById('cSpeed').value)||0,
    speed_limit_unit:'MBIT'
  };
  const r=await api('/api/links',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r){showResult(r);refreshAll()}
}
async function doChangePw(){
  const cur=document.getElementById('pwCur').value,nw=document.getElementById('pwNew').value,cf=document.getElementById('pwCf').value;
  if(nw!==cf){toast(lang==='fa'?'رمزها یکی نیستند':'Passwords mismatch');return}
  const r=await api('/api/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:cur,new_password:nw,repeat_password:cf})});
  if(r){toast(lang==='fa'?'رمز تغییر کرد':'Password changed');document.getElementById('pwCur').value='';document.getElementById('pwNew').value='';document.getElementById('pwCf').value=''}
}
async function loadLogs(){
  const box=document.getElementById('logsBox');
  const data=await api('/api/activity');
  const logs=Array.isArray(data)?data:(data&&data.logs)||[];
  if(!logs.length){box.innerHTML=`<div style="text-align:center;color:var(--t3);padding:24px">${lang==='fa'?'لاگی نیست':'No logs'}</div>`;return}
  box.innerHTML=logs.slice().reverse().map(l=>{
    const tm=(l.time||l.ts||'').toString().slice(11,19)||'—';
    return `<div class="log-item"><div class="log-time">${esc(tm)}</div><div class="log-msg">${esc(l.message||l.msg||JSON.stringify(l))}</div></div>`;
  }).join('');
}
function setRange(r,el){
  statRange=r;
  document.querySelectorAll('#rangeTabs .range-tab').forEach(t=>t.classList.toggle('on',t.dataset.r===r));
  refreshAll();toast(t('r_'+r));
}
function randomName(){
  const chars='abcdefghijklmnopqrstuvwxyz0123456789';
  let s='';
  for(let i=0;i<10;i++) s+=chars[Math.floor(Math.random()*chars.length)];
  if(/^[0-9]/.test(s)) s='a'+s.slice(1);
  document.getElementById('cName').value=s;
}
async function panelUpdate(){
  const m=document.getElementById('panelModal');
  const t=document.getElementById('panelModalTitle');
  const b=document.getElementById('panelModalBody');
  t.textContent=lang==='fa'?'در حال بررسی آپدیت...':'Checking update...';
  b.innerHTML='<div style="text-align:center;padding:20px"><div class="spin"></div></div>';
  m.classList.add('open');
  try{
    const r=await api('/api/system/update-check');
    if(!r){ b.innerHTML='<p>خطا در ارتباط با سرور</p>'; return; }
    t.textContent=lang==='fa'?'آپدیت AGN021G':'AGN021G Update';
    if(r.up_to_date){
      b.innerHTML=`<p style="margin-bottom:12px">✅ پنل به‌روز است<br><small>نسخه فعلی: ${r.current||''}</small></p>
        <p style="font-size:12px;opacity:.7">مخزن: <a href="${r.github||'https://github.com/agn021g/PANEL-AGN021G'}" target="_blank" style="color:#93c5fd">GitHub AGN021G</a></p>`;
    } else if(r.available){
      b.innerHTML=`<p style="margin-bottom:12px">🚀 نسخه جدید: <b>${r.remote||''}</b><br>نسخه فعلی: ${r.current||''}</p>
        <p style="margin-bottom:12px;font-size:13px">${r.notes||''}</p>
        <button class="btn btn-primary" type="button" onclick="panelApplyUpdate()">دانلود و اعمال آپدیت</button>
        <p style="margin-top:10px;font-size:11px;opacity:.7">پس از آپدیت سرویس روی Railway ری‌استارت می‌شود.</p>`;
    } else {
      b.innerHTML=`<p style="margin-bottom:12px">${r.message||'نتیجه‌ای دریافت نشد'}</p>
        <p style="font-size:12px"><a href="https://github.com/agn021g/PANEL-AGN021G" target="_blank" style="color:#93c5fd">مشاهده مخزن گیت‌هاب</a></p>`;
    }
  }catch(e){
    b.innerHTML='<p>خطا در بررسی آپدیت. دستی از GitHub بروزرسانی کنید.</p>';
  }
}
async function panelApplyUpdate(){
  const b=document.getElementById('panelModalBody');
  b.innerHTML='<div style="text-align:center;padding:20px"><div class="spin"></div><p>در حال دانلود و اعمال...</p></div>';
  const r=await api('/api/system/update-apply',{method:'POST'});
  if(r&&r.ok){
    b.innerHTML=`<p>✅ ${r.message||'آپدیت اعمال شد. سرویس را ری‌استارت کنید.'}</p>`;
  } else {
    b.innerHTML=`<p>❌ ${(r&&r.message)||'اعمال آپدیت ناموفق بود'}</p>`;
  }
}
async function saveTelegram(){
  const token=document.getElementById('tgToken').value.trim();
  const admin=document.getElementById('tgAdmin').value.trim();
  const webhook=document.getElementById('tgWebhook').checked;
  if(!token||!admin){toast(lang==='fa'?'توکن و آیدی لازم است':'Token and admin ID required');return}
  toast(lang==='fa'?'در حال فعال‌سازی...':'Activating...');
  const r=await api('/api/telegram/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,admin_ids:admin,webhook})});
  if(r){
    document.getElementById('tgStatus').textContent=r.message||(lang==='fa'?'فعال شد':'Enabled');
    toast(r.message||'OK');
  }
}
async function loadTelegram(){
  const r=await api('/api/telegram/settings');
  if(!r)return;
  if(r.admin_ids) document.getElementById('tgAdmin').value=r.admin_ids;
  document.getElementById('tgWebhook').checked=r.webhook!==false;
  document.getElementById('tgStatus').textContent=r.has_token?(lang==='fa'?'توکن ذخیره شده: ':'Token saved: ')+(r.token_masked||''):'';
}
const _goPage=goPage;
goPage=function(name){
  _goPage(name);
  if(name==='telegram') loadTelegram();
  if(name==='news') loadNews();
  if(name==='admins') loadAdmins();
  if(name==='groups') loadGroups();
  if(name==='users') loadUsers();
  if(name==='settings'){loadSecurity();loadPanelPath();loadNetwork();loadMtproto();}
};

const PERM_LABELS={
  fa:{dash:'داشبورد',configs:'کانفیگ‌ها',create:'ساخت',stats:'آمار',logs:'لاگ',settings:'تنظیمات',support:'پشتیبانی',telegram:'ربات',news:'اخبار',admins:'ادمین‌ها'},
  en:{dash:'Dashboard',configs:'Configs',create:'Create',stats:'Stats',logs:'Logs',settings:'Settings',support:'Support',telegram:'Bot',news:'News',admins:'Admins'}
};
let USER_PERMS=null;
let USER_ROLE='owner';
function buildPermChecks(containerId, selected){
  const box=document.getElementById(containerId);
  if(!box)return;
  const labels=PERM_LABELS[lang]||PERM_LABELS.fa;
  box.innerHTML=Object.keys(labels).map(k=>{
    const on=selected?!!selected[k]:(['dash','configs','create','stats','news'].includes(k));
    return `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:12px;background:var(--bg3);border:1px solid var(--card-b)">
      <span style="font-size:12px;font-weight:600">${labels[k]}</span>
      <label class="switch"><input type="checkbox" data-perm="${k}" ${on?'checked':''}><span class="slider"></span></label>
    </div>`;
  }).join('');
}
function readPermChecks(containerId){
  const out={};
  document.querySelectorAll('#'+containerId+' input[data-perm]').forEach(inp=>{out[inp.getAttribute('data-perm')]=inp.checked});
  return out;
}
async function loadMe(){
  const r=await api('/api/me');
  if(!r)return;
  USER_ROLE=r.role||'owner';
  USER_PERMS=r.permissions||{};
  document.querySelectorAll('.nav-item[data-perm]').forEach(el=>{
    const p=el.getAttribute('data-perm');
    if(USER_ROLE==='owner'){el.style.display='';return}
    el.style.display=USER_PERMS[p]?'':'none';
  });
  // hide admins for non-owner always if no perm
  document.querySelectorAll('.nav-item[data-page="admins"]').forEach(el=>{
    if(USER_ROLE!=='owner') el.style.display='none';
  });
}
async function loadNews(toastOk){
  const r=await api('/api/news');
  if(!r)return;
  document.getElementById('newsTitle').textContent=r.title||(lang==='fa'?'بدون عنوان':'No title');
  document.getElementById('newsBody').textContent=r.message||'';
  document.getElementById('newsMeta').textContent=(lang==='fa'?'بروزرسانی: ':'Updated: ')+(r.updated_at||'—');
  if(toastOk) toast(lang==='fa'?'اطلاعیه بروزرسانی شد':'News refreshed');
}
async function loadAdmins(){
  buildPermChecks('adPerms');
  const r=await api('/api/admins');
  const box=document.getElementById('adminsList');
  if(!r||!r.admins){box.innerHTML='<div style="color:var(--t3);text-align:center;padding:20px">—</div>';return}
  if(!r.admins.length){box.innerHTML=`<div style="color:var(--t3);text-align:center;padding:20px">${lang==='fa'?'ادمینی نیست':'No admins'}</div>`;return}
  const labels=PERM_LABELS[lang]||PERM_LABELS.fa;
  box.innerHTML=r.admins.map(a=>{
    const st=a.blocked?'🔴 مسدود':(a.valid?'🟢 فعال':'🟠 نامعتبر');
    const perms=Object.entries(a.permissions||{}).filter(([,v])=>v).map(([k])=>labels[k]||k).join(' · ')||'—';
    return `<div style="border:1px solid var(--card-b);border-radius:12px;padding:12px;margin-bottom:10px;background:var(--bg3)">
      <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center">
        <div><b>${esc(a.username)}</b> <span style="font-size:11px;color:var(--t3)">${st}</span></div>
        <div class="ops" style="align-items:center">
          <label class="switch" title="مسدود">
            <input type="checkbox" ${a.blocked?'checked':''} onchange="toggleBlockAdmin('${esc(a.id)}',this.checked)">
            <span class="slider"></span>
          </label>
          <button class="btn btn-sm btn-d" onclick="deleteAdmin('${esc(a.id)}')">حذف</button>
        </div>
      </div>
      <div style="font-size:11px;color:var(--t3);margin-top:8px">حجم: ${fmtB(a.used_bytes)}${a.limit_bytes?(' / '+fmtB(a.limit_bytes)):' / ∞'} · انقضا: ${a.expires_at||'∞'}</div>
      <div style="font-size:11px;color:var(--t2);margin-top:6px">${perms}</div>
    </div>`;
  }).join('');
}
async function createAdmin(){
  const body={
    username:document.getElementById('adUser').value.trim(),
    password:document.getElementById('adPw').value,
    repeat_password:document.getElementById('adPw2').value,
    limit_value:Number(document.getElementById('adLimit').value)||0,
    limit_unit:document.getElementById('adUnit').value,
    expires_days:Number(document.getElementById('adDays').value)||0,
    permissions:readPermChecks('adPerms')
  };
  const r=await api('/api/admins',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r){toast(lang==='fa'?'اکانت ساخته شد':'Created');document.getElementById('adUser').value='';document.getElementById('adPw').value='';document.getElementById('adPw2').value='';loadAdmins()}
}
async function toggleBlockAdmin(id,blocked){
  const r=await api('/api/admins/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({blocked})});
  if(r){toast(blocked?'مسدود شد':'رفع شد');loadAdmins()}
}
async function deleteAdmin(id){
  if(!confirm(lang==='fa'?'حذف اکانت؟':'Delete?'))return;
  const r=await api('/api/admins/'+id,{method:'DELETE'});
  if(r){toast('OK');loadAdmins()}
}


async function loadProtocols(){
  const r=await api('/api/protocols');
  const list=(r&&r.protocols)||[];
  const def=(r&&r.default)||'vless-ws';
  window._PROTOCOLS=list.map(p=>p.id);
  const el=document.getElementById('cProto');
  if(el){
    el.innerHTML=list.map(p=>`<option value="${esc(p.id)}" ${p.id===def?'selected':''}>${esc(p.label||p.id)}</option>`).join('')
      ||'<option value="vless-ws">VLESS WebSocket</option>';
  }
  fillMultiProtoList();
}
let __allLinks=[];
function filterConfigs(){
  const q=(document.getElementById('cfgSearch')?.value||'').trim().toLowerCase();
  if(!q){renderLinks(__allLinks);return}
  renderLinks(__allLinks.filter(l=>{
    const name=(l.label||l.name||'').toLowerCase();
    const proto=(l.protocol||'').toLowerCase();
    const uid=String(l.uuid||l.id||'').toLowerCase();
    return name.includes(q)||proto.includes(q)||uid.includes(q);
  }));
}
async function resetUsage(uid){
  if(!confirm(lang==='fa'?'مصرف ریست شود؟':'Reset usage?'))return;
  const r=await api('/api/links/'+uid+'/reset-usage',{method:'POST'});
  if(r!==null){toast(lang==='fa'?'مصرف ریست شد':'Usage reset');refreshAll()}
}


let __dragUid=null;
function cfgDragStart(e){__dragUid=e.currentTarget.getAttribute('data-uid');e.currentTarget.style.opacity='.5';e.dataTransfer.effectAllowed='move';}
function cfgDragOver(e){e.preventDefault();e.dataTransfer.dropEffect='move';const tr=e.currentTarget;if(tr&&tr.tagName==='TR')tr.style.background='var(--hover)';}
function cfgDragEnd(e){e.currentTarget.style.opacity='1';document.querySelectorAll('#linksTable tr').forEach(tr=>tr.style.background='');}
async function cfgDrop(e){
  e.preventDefault();
  const target=e.currentTarget.getAttribute('data-uid');
  document.querySelectorAll('#linksTable tr').forEach(tr=>tr.style.background='');
  if(!__dragUid||!target||__dragUid===target)return;
  const rows=[...document.querySelectorAll('#linksTable tr[data-uid]')];
  const ids=rows.map(r=>r.getAttribute('data-uid'));
  const from=ids.indexOf(__dragUid), to=ids.indexOf(target);
  if(from<0||to<0)return;
  ids.splice(from,1);ids.splice(to,0,__dragUid);
  // reorder DOM optimistically
  const tb=document.getElementById('linksTable');
  ids.forEach(id=>{const el=tb.querySelector(`tr[data-uid="${id}"]`);if(el)tb.appendChild(el);});
  await api('/api/links/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:ids})});
  toast(lang==='fa'?'ترتیب ذخیره شد':'Order saved');
}

function updateBulkBar(){
  const n=document.querySelectorAll('.cfg-chk:checked').length;
  const bar=document.getElementById('bottomBulkBar');
  const cnt=document.getElementById('bulkCount');
  if(cnt) cnt.textContent = n + (lang==='fa'?' انتخاب‌شده':' selected');
  if(bar) bar.classList.toggle('show', n>0);
  const all=document.getElementById('chkAll');
  if(all && n===0) all.checked=false;
}
function clearSelection(){
  document.querySelectorAll('.cfg-chk').forEach(c=>c.checked=false);
  const all=document.getElementById('chkAll');
  if(all) all.checked=false;
  updateBulkBar();
}
function toggleSelectAll(on){
  document.querySelectorAll('.cfg-chk').forEach(c=>c.checked=!!on);
  updateBulkBar();
}

function selectedCfgIds(){return [...document.querySelectorAll('.cfg-chk:checked')].map(c=>c.value)}
async function bulkDelete(){
  const ids=selectedCfgIds();
  if(!ids.length){toast(lang==='fa'?'چیزی انتخاب نشده':'Nothing selected');return}
  if(!confirm(lang==='fa'?`حذف ${ids.length} کانفیگ؟`:`Delete ${ids.length}?`))return;
  const r=await api('/api/links/bulk-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
  if(r){toast(lang==='fa'?`حذف شد: ${r.deleted}`:`Deleted: ${r.deleted}`);refreshAll()}
}
async function bulkMoveGroup(){
  const ids=selectedCfgIds();
  const cid=document.getElementById('bulkGroup')?.value||'0';
  if(!ids.length){toast(lang==='fa'?'چیزی انتخاب نشده':'Nothing selected');return}
  const r=await api('/api/links/bulk-category',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,category_id:cid})});
  if(r){toast(lang==='fa'?'به گروه منتقل شد':'Moved');refreshAll()}
}

let __allUsers=[];
async function loadUsers(){
  const data=await api('/api/subs');
  const list=(data&&data.subs)||[];
  __allUsers=list;
  const total=list.length;
  const active=list.filter(s=>(s.active_count||0)>0).length;
  const links=list.reduce((a,s)=>a+(s.links_count||0),0);
  const traffic=list.reduce((a,s)=>a+(s.total_used_bytes||0),0);
  const el=id=>document.getElementById(id);
  if(el('uTotal')) el('uTotal').textContent=total;
  if(el('uActive')) el('uActive').textContent=active;
  if(el('uLinks')) el('uLinks').textContent=links;
  if(el('uTraffic')) el('uTraffic').textContent=fmtB(traffic);
  renderUsers(list);
}
function fmtB(n){
  n=Number(n)||0;
  if(n<1024)return n+' B';
  if(n<1048576)return (n/1024).toFixed(1)+' KB';
  if(n<1073741824)return (n/1048576).toFixed(2)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
function filterUsers(){
  const q=(document.getElementById('userSearch')?.value||'').trim().toLowerCase();
  if(!q){renderUsers(__allUsers);return}
  renderUsers(__allUsers.filter(s=>String(s.name||'').toLowerCase().includes(q)));
}
function renderUsers(list){
  const tb=document.getElementById('usersBody');
  if(!tb)return;
  if(!list.length){
    tb.innerHTML=`<tr><td colspan="6" style="text-align:center;color:var(--t3);padding:28px">کاربری نیست — از بخش ساخت، ساب چندپروتکلی بسازید</td></tr>`;
    return;
  }
  tb.innerHTML=list.map(s=>{
    const sid=esc(s.sub_id);
    const name=esc(s.name||'—');
    const usage=esc(s.usage_fmt||s.total_used_fmt||'—');
    const cnt=`${s.active_count||0}/${s.links_count||0}`;
    let exp='∞';
    if(s.expires_at){
      try{exp=new Date(s.expires_at).toLocaleDateString('fa-IR')}catch(e){exp=String(s.expires_at).slice(0,10)}
    }
    const on=(s.active_count||0)>0;
    const status=on
      ?'<span style="color:#34d399;font-weight:700;font-size:12px">فعال</span>'
      :'<span style="color:#f87171;font-weight:700;font-size:12px">قطع</span>';
    const protos=(s.protocols||[]).slice(0,3).map(p=>esc(p)).join(' · ');
    return `<tr>
      <td style="min-width:140px">
        <div style="font-weight:700;font-size:13px">${name}</div>
        <div style="font-size:10px;color:var(--t3);margin-top:2px">${protos}</div>
      </td>
      <td style="font-size:12px;direction:ltr;text-align:right">${usage}</td>
      <td style="text-align:center;font-size:12px">${cnt}</td>
      <td style="font-size:12px">${exp}</td>
      <td>${status}</td>
      <td>
        <div style="display:flex;flex-wrap:wrap;gap:4px;justify-content:center">
          <button class="btn btn-sm" title="کپی لینک" onclick="copyText('${esc(s.sub_url||s.public_url||'')}')">کپی</button>
          <button class="btn btn-sm" title="باز کردن" onclick="window.open('${esc(s.sub_url||s.public_url||'#')}','_blank')">باز</button>
          <button class="btn btn-sm" style="color:#34d399" onclick="userEnable('${sid}')">فعال</button>
          <button class="btn btn-sm" style="color:#fbbf24" onclick="userDisable('${sid}')">قطع</button>
          <button class="btn btn-sm" onclick="userExtend('${sid}')">تمدید</button>
          <button class="btn btn-sm" onclick="userReset('${sid}')">ریست</button>
          <button class="btn btn-sm" onclick="userQuota('${sid}')">سهمیه</button>
          <button class="btn btn-sm btn-d" onclick="userDelete('${sid}','${name}')">حذف</button>
        </div>
      </td>
    </tr>`;
  }).join('');
}
async function copyText(t){
  try{
    if(navigator.clipboard&&window.isSecureContext) await navigator.clipboard.writeText(t);
    else{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
    toast(lang==='fa'?'کپی شد':'Copied');
  }catch(e){toast('خطا')}
}
async function userEnable(id){
  const r=await api('/api/subs/'+id+'/enable',{method:'POST'});
  if(r){toast('فعال شد');loadUsers();refreshAll()}
}
async function userDisable(id){
  if(!confirm('همه کانفیگ‌های این کاربر قطع شوند؟'))return;
  const r=await api('/api/subs/'+id+'/disable',{method:'POST'});
  if(r){toast('قطع شد');loadUsers();refreshAll()}
}
async function userExtend(id){
  const days=prompt('چند روز تمدید شود؟','30');
  if(days===null)return;
  const n=Math.max(1,Math.min(3650,Number(days)||30));
  const r=await api('/api/subs/'+id+'/extend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:n})});
  if(r){toast(n+' روز تمدید شد');loadUsers()}
}
async function userReset(id){
  if(!confirm('مصرف این کاربر ریست شود؟'))return;
  const r=await api('/api/subs/'+id+'/reset-usage',{method:'POST'});
  if(r){toast('مصرف ریست شد');loadUsers();refreshAll()}
}
async function userQuota(id){
  const gb=prompt('محدودیت حجم (GB) — 0 = نامحدود','0');
  if(gb===null)return;
  const ip=prompt('محدودیت IP — 0 = نامحدود','0');
  if(ip===null)return;
  const r=await api('/api/subs/'+id+'/set-quota',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit_value:Number(gb)||0,limit_unit:'GB',ip_limit:Number(ip)||0})});
  if(r){toast('سهمیه ذخیره شد');loadUsers()}
}
async function userDelete(id,name){
  if(!confirm('کاربر «'+name+'» حذف شود؟ (کانفیگ‌ها از گروه خارج می‌شوند)'))return;
  const r=await api('/api/subs/'+id,{method:'DELETE'});
  if(r){toast('حذف شد');loadUsers();refreshAll()}
}

async function loadGroups(){
  const r=await api('/api/categories');
  const list=(r&&r.categories)||[];
  window.__catMap={};
  list.forEach(g=>{window.__catMap[String(g.id)]=g.name||g.id});
  const bulk=document.getElementById('bulkGroup');
  const cGroup=document.getElementById('cGroup');
  const opts=list.map(g=>`<option value="${esc(g.id)}">${esc(g.name||g.id)}</option>`).join('');
  if(bulk) bulk.innerHTML=opts||'<option value="0">عمومی</option>';
  if(cGroup) cGroup.innerHTML=opts||'<option value="0">عمومی</option>';
  const box=document.getElementById('groupsList');
  if(box){
    if(!list.length){box.innerHTML='<div style="color:var(--t3);text-align:center;padding:16px">—</div>';}
    else{
      box.innerHTML=list.map(g=>{
        const cnt=(__allLinks||[]).filter(l=>String(l.category_id||'0')===String(g.id)).length;
        return `<div style="border:1px solid var(--card-b);border-radius:12px;padding:12px;margin-bottom:8px;background:var(--bg3);display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap">
          <div><b>${esc(g.name)}</b> <span style="font-size:11px;color:var(--t3)">${cnt} کانفیگ</span></div>
          <button class="btn btn-sm btn-d" onclick="deleteGroup('${esc(g.id)}')">حذف</button>
        </div>`;
      }).join('');
    }
  }
}
async function createGroup(){
  const name=document.getElementById('grpName').value.trim();
  if(!name){toast('نام لازم است');return}
  const r=await api('/api/categories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  if(r){toast('گروه ساخته شد');document.getElementById('grpName').value='';loadGroups()}
}
async function deleteGroup(id){
  if(!confirm('حذف گروه؟'))return;
  const r=await api('/api/categories/'+id,{method:'DELETE'});
  if(r){toast('حذف شد');loadGroups();refreshAll()}
}






function mtRowHtml(p, idx){
  p=p||{};
  return `<div class="mt-row" data-i="${idx}" style="padding:12px;border-radius:14px;border:1px solid var(--card-b);background:var(--bg3)">
    <div class="form-row" style="margin-bottom:8px">
      <div class="field" style="flex:1"><label>نام</label><input class="mtName" value="${esc(p.name||('پروکسی '+(idx+1)))}" placeholder="پروکسی ${idx+1}"></div>
      <div class="field" style="flex:1"><label>پورت</label><input class="mtPort" type="number" value="${esc(p.port||443)}" style="direction:ltr;text-align:left"></div>
    </div>
    <div class="field"><label>سرور</label><input class="mtServer" value="${esc(p.server||'')}" placeholder="roundhouse.proxy.rlwy.net" style="direction:ltr;text-align:left"></div>
    <div class="field"><label>Secret</label><input class="mtSecret" value="${esc(p.secret||'')}" placeholder="hex secret" style="direction:ltr;text-align:left;font-family:ui-monospace,monospace;font-size:11px"></div>
    <button type="button" class="btn btn-sm btn-d" style="margin-top:6px" onclick="this.closest('.mt-row').remove()">حذف</button>
  </div>`;
}
function addMtRow(p){
  const box=document.getElementById('mtList');
  if(!box)return;
  const idx=box.querySelectorAll('.mt-row').length;
  box.insertAdjacentHTML('beforeend', mtRowHtml(p, idx));
}
async function loadMtproto(){
  const r=await api('/api/mtproto');
  if(!r)return;
  const en=document.getElementById('mtEnabled');
  if(en) en.checked=!!r.enabled;
  const box=document.getElementById('mtList');
  if(box){
    box.innerHTML='';
    const list=(r.proxies&&r.proxies.length)?r.proxies:[{name:'پروکسی ۱',server:'',port:443,secret:''}];
    list.forEach((p,i)=>addMtRow(p));
  }
  const links=document.getElementById('mtLinks');
  if(links){
    if(r.proxies&&r.proxies.length){
      links.innerHTML=r.proxies.map(p=>`<div style="margin-bottom:10px"><b>${esc(p.name)}</b><br>${esc(p.https_link)}<br><button type="button" class="btn btn-sm" style="margin-top:4px" onclick="navigator.clipboard.writeText('${esc(p.https_link)}');toast('کپی شد')">کپی</button></div>`).join('');
    }else links.textContent='هنوز پروکسی کامل وارد نشده (اختیاری است)';
  }
}
async function saveMtproto(){
  const proxies=[];
  document.querySelectorAll('#mtList .mt-row').forEach(row=>{
    proxies.push({
      name:(row.querySelector('.mtName')||{}).value||'',
      server:(row.querySelector('.mtServer')||{}).value||'',
      port:parseInt((row.querySelector('.mtPort')||{}).value||'443',10)||443,
      secret:(row.querySelector('.mtSecret')||{}).value||'',
    });
  });
  const body={enabled:!!(document.getElementById('mtEnabled')||{}).checked, proxies};
  const r=await api('/api/mtproto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r&&r.ok){toast(lang==='fa'?'ذخیره شد':'Saved');loadMtproto()}
}

async function loadNetwork(){
  const r=await api('/api/network');
  if(!r)return;
  const h=document.getElementById('netHost');
  const p=document.getElementById('netPort');
  const s=document.getElementById('netSec');
  const v6=document.getElementById('netIpv6');
  const ef=document.getElementById('netEffective');
  if(h) h.value=r.public_host||'';
  if(p) p.value=r.public_port||'';
  if(s) s.value=r.public_security||'tls';
  if(v6) v6.checked=!!r.prefer_ipv6;
  if(ef) ef.textContent='effective: '+(r.sample||'');
}
async function saveNetwork(){
  const body={
    public_host:(document.getElementById('netHost')||{}).value||'',
    public_port:parseInt((document.getElementById('netPort')||{}).value||'0',10)||0,
    public_security:(document.getElementById('netSec')||{}).value||'tls',
    prefer_ipv6:!!(document.getElementById('netIpv6')||{}).checked,
  };
  const r=await api('/api/network',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r&&r.ok){toast(lang==='fa'?'ذخیره شد':'Saved');loadNetwork();}
}

async function loadPanelPath(){
  const r=await api('/api/panel-path');
  if(!r)return;
  const path=r.path||'';
  const login=r.login_url||((window.PANEL_BASE||'')+'/login');
  const el=document.getElementById('panelPathShow');
  const el2=document.getElementById('panelLoginUrl');
  if(el) el.value='/'+path;
  if(el2) el2.value=login;
  const note=document.getElementById('panelPathEnvNote');
  if(note) note.style.display=r.from_env?'block':'none';
  const inp=document.getElementById('panelPathNew');
  if(inp&&!inp.dataset.touched) inp.placeholder=path;
}
function copyPanelPath(){
  const v=(document.getElementById('panelPathShow')||{}).value||'';
  if(typeof copyText==='function') copyText(v); else {navigator.clipboard&&navigator.clipboard.writeText(v);toast('کپی شد');}
}
function copyPanelLogin(){
  const v=(document.getElementById('panelLoginUrl')||{}).value||'';
  if(typeof copyText==='function') copyText(v); else {navigator.clipboard&&navigator.clipboard.writeText(v);toast('کپی شد');}
}
async function savePanelPath(){
  const inp=document.getElementById('panelPathNew');
  const v=(inp&&inp.value||'').trim().replace(/^\/+|\/+$/g,'');
  if(!v){toast(lang==='fa'?'مسیر جدید را وارد کنید':'Enter new path');return}
  if(!/^[A-Za-z0-9_-]{4,64}$/.test(v)){toast(lang==='fa'?'مسیر نامعتبر (۴–۶۴ حرف/عدد/-/_)':'Invalid path');return}
  if(!confirm(lang==='fa'?'مسیر پنل عوض شود؟ بعد باید با لینک جدید وارد شوید.':'Change panel path? You must login with the new URL.'))return;
  const r=await api('/api/panel-path',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:v})});
  if(r&&r.ok){
    toast(r.message||'OK');
    // redirect to new login
    const newBase='/'+v;
    window.PANEL_BASE=newBase;
    setTimeout(()=>{location.href=newBase+'/login'},800);
  }
}

async function loadSecurity(){
  const r=await api('/api/security/status');
  const el=document.getElementById('secStatus');
  if(!r||!el)return;
  const locked=(r.locked_ips||[]).map(x=>`${x.ip} (${Math.ceil(x.remaining_sec/60)}د)`).join(' · ')||'—';
  el.innerHTML=`حداکثر تلاش: <b>${r.max_attempts}</b> · قفل: <b>${Math.round(r.lockout_seconds/60)} دقیقه</b><br>IPهای مسدود: ${locked}`;
}
async function unlockAllIps(){
  if(!confirm('رفع مسدودی همه؟'))return;
  const r=await api('/api/security/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  if(r){toast('انجام شد');loadSecurity()}
}


async function downloadBackup(kind){
  try{
    const url = kind==='bot' ? '/api/backup/bot' : '/api/backup/users';
    const r = await fetch(panelUrl(url), {credentials:'same-origin', cache:'no-store'});
    if(r.status===401){ location.href=panelUrl('/login'); return; }
    if(!r.ok){
      let msg='خطا';
      try{ const j=await r.json(); msg=j.detail||msg; }catch(e){}
      toast(String(msg)); return;
    }
    const text = await r.text();
    // validate json
    try{ JSON.parse(text); }catch(e){ toast('پاسخ نامعتبر'); return; }
    const blob = new Blob([text], {type:'application/json;charset=utf-8'});
    const a = document.createElement('a');
    const stamp = new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
    a.href = URL.createObjectURL(blob);
    a.download = kind==='bot' ? ('panel-bot-'+stamp+'.json') : ('panel-users-'+stamp+'.json');
    document.body.appendChild(a);
    a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 500);
    toast(lang==='fa'?'دانلود شد':'Downloaded');
  }catch(e){ toast(String(e.message||e)); }
}
function readJsonFile(inputId){
  return new Promise((resolve,reject)=>{
    const inp=document.getElementById(inputId);
    if(!inp||!inp.files||!inp.files[0]){ reject(new Error(lang==='fa'?'فایل انتخاب نشده':'No file')); return; }
    const fr=new FileReader();
    fr.onload=()=>{ try{ resolve(JSON.parse(fr.result)); }catch(e){ reject(new Error('JSON نامعتبر')); } };
    fr.onerror=()=>reject(new Error('خواندن فایل ناموفق'));
    fr.readAsText(inp.files[0],'utf-8');
  });
}
async function restoreUsers(mode){
  try{
    const data = await readJsonFile('restoreUsersFile');
    data.mode = mode||'merge';
    if(mode==='replace' && !confirm(lang==='fa'?'همه داده‌های فعلی پاک و جایگزین می‌شود. مطمئنی؟':'Replace all current data?')) return;
    const r = await api('/api/restore/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(r){ toast(lang==='fa'?('بازیابی شد: '+r.links+' کانفیگ'):('Restored: '+r.links)); refreshAll(); }
  }catch(e){ toast(e.message||String(e)); }
}
async function restoreBot(){
  try{
    const data = await readJsonFile('restoreBotFile');
    const r = await api('/api/restore/bot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(r) toast(r.message||(lang==='fa'?'ربات بازیابی شد':'Bot restored'));
  }catch(e){ toast(e.message||String(e)); }
}

applyLang();loadMe();loadProtocols();loadGroups();loadPanelPath();loadNetwork();loadMtproto();refreshAll();setInterval(refreshAll,1000);


</script>
</body>
</html>
"""




@app.get(
    "/dashboard",
    response_class=HTMLResponse,
)
async def dashboard(
    request: Request,
):

    if not await is_valid_session(
        request.cookies.get(
            SESSION_COOKIE
        )
    ):
        return RedirectResponse(f"/{PANEL_PATH}/login")

    await ensure_default_categories()
    await ensure_default_link()

    return HTMLResponse(
        inject_panel_base(DASHBOARD_HTML, request)
    )


# ============================================================
# TEST
# ============================================================

@app.get(
    "/test-ws",
    response_class=HTMLResponse,
)
async def test_ws():

    return HTMLResponse(
        """
        <script>
        location.href='/'  // rewritten under panel path
        </script>
        """
    )


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc: Exception,
):

    stats[
        "total_errors"
    ] += 1

    error_logs.append(
        {
            "error":
                str(exc),

            "path":
                str(request.url),

            "method":
                request.method,

            "time":
                datetime.now().isoformat(),
        }
    )

    logger.exception(
        "Unhandled exception: %s %s",
        request.method,
        request.url,
    )

    # API requests
    if (
        request.url.path.startswith(
            "/api/"
        )
        or request.url.path == "/stats"
    ):

        return JSONResponse(
            {
                "ok": False,
                "error":
                    str(exc)
                or "internal server error",
            },
            status_code=500,
        )

    return HTMLResponse(
        """
        <html lang="fa" dir="rtl">
        <body style="
            background:#07070a;
            color:#fff;
            font-family:sans-serif;
            padding:40px;
        ">
            <h2>
            خطای داخلی AGN021G
            </h2>

            <p>
            لطفاً لاگ Railway را بررسی کنید.
            </p>
        </body>
        </html>
        """,
        status_code=500,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        workers=1,
    )
