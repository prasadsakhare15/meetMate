"""Login/register against the cloud server's REST auth endpoints.

Deliberately stdlib-only (urllib): the agent has no HTTP client dependency,
and these two POSTs do not justify adding one. The WebSocket URL from the
environment is converted to its http(s) base, so ``CLOUD_WS_URL`` stays the
single place the cloud server is configured.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class AuthError(Exception):
    """A login/register request failed; the message is safe to show the user."""


def _http_base_url() -> str:
    """Turn ``ws://host:port/path`` into ``http://host:port/path-parent``."""

    ws_url = os.getenv("CLOUD_WS_URL", "ws://127.0.0.1:8080/ws")

    scheme, _, rest = ws_url.partition("://")
    base = rest.rsplit("/", 1)[0]

    if scheme == "wss":
        return f"https://{base}"
    if scheme == "ws":
        return f"http://{base}"
    if scheme:
        return f"{scheme}://{base}"
    return base


def _post(path: str, payload: dict) -> dict:
    """POST JSON and return the parsed response, raising AuthError on failure."""

    request = urllib.request.Request(
        _http_base_url() + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        # FastAPI errors carry a JSON ``detail`` meant for humans.
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        raise AuthError(detail or f"Server error (HTTP {e.code}).") from None

    except urllib.error.URLError as e:
        raise AuthError(f"Cannot reach the server: {e.reason}") from None


def login(username: str, password: str) -> dict:
    """Exchange credentials for a token. Returns the server's TokenResponse."""

    return _post("/auth/login", {"username": username, "password": password})


def register(username: str, password: str) -> dict:
    """Create an account and get a token back in the same call."""

    return _post("/auth/register", {"username": username, "password": password})


def update_password(username: str, old_password: str, new_password: str) -> dict:
    """Change the user's password."""
    
    return _post("/auth/update-password", {
        "username": username, 
        "old_password": old_password, 
        "new_password": new_password
    })
