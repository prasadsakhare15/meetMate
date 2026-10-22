"""WebSocket token extraction + verification helpers.

Everything here is stateless: given a ``WebSocket`` upgrade, we pull the JWT
from the query string (or the first inbound frame) and verify it. There is no
per-connection state and no custom middleware class.
"""

import asyncio
import json
import re

from fastapi import WebSocket

from users import get_user, verify_token

# How long to wait for a token in the first frame before giving up.
FIRST_FRAME_TIMEOUT = 10  # seconds

# Token may be passed in the Authorization header, the WS query string, or as
# the first JSON frame. The header is checked first.
_ws_token_query_re = re.compile(r"(?i)^(?:token|auth)=([^&]+)$")
_bearer_re = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


def _extract_token(query_string) -> str | None:
    """Pull a bearer token out of the WebSocket query string."""
    if not query_string:
        return None
    # FastAPI passes query_string as bytes (b'token=...') — decode it.
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", errors="replace")
    match = _ws_token_query_re.search(query_string)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    return None


def _extract_bearer(header_value) -> str | None:
    """Pull the token out of an ``Authorization: Bearer ...`` header."""
    if not header_value:
        return None
    match = _bearer_re.match(header_value.strip())
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    return None


async def get_verified_user_id(websocket: WebSocket) -> str | None:
    """Return the authenticated ``user_id`` for this socket, or ``None``.

    The token is looked up in order:

    1. ``Authorization: Bearer <token>`` header,
    2. ``token`` in the query string (``/ws?token=...``),
    3. the first inbound JSON frame (``{"token": ...}``).

    On success the JWT's ``sub`` is checked against the users table and
    returned. On failure the socket is closed with close code 4401 and
    ``None`` is returned.
    """

    token = _extract_bearer(websocket.headers.get("authorization"))

    if token is None:
        token = _extract_token(
            websocket.scope.get("query_string", b"").decode("utf-8", errors="replace")
        )

    if token is None:
        message = await _receive_first_frame(websocket)

        while True:
            if message is None:
                await _reject(websocket)
                return None
            try:
                type_ = message.get("type")
            except Exception:
                await _reject(websocket)
                return None
            if type_ == "websocket.connect":
                message = await _receive_first_frame(websocket)
                continue
            if type_ == "websocket.disconnect":
                await _reject(websocket)
                return None
            if type_ == "websocket.receive":
                try:
                    raw = message["text"]
                except KeyError:
                    await _reject(websocket)
                    return None
                try:
                    data = json.loads(raw)
                except Exception:
                    await _reject(websocket)
                    return None
                if isinstance(data, dict) and data.get("token"):
                    token = data["token"]
                    break
                await _reject(websocket)
                return None
            await _reject(websocket)
            return None

    payload = verify_token(token)
    if payload is None:
        await _reject(websocket)
        return None

    user_id = payload.get("sub")
    if not user_id:
        await _reject(websocket)
        return None

    # Look up the user in SQLite to be sure they still exist (and get their id).
    user = get_user(user_id)
    if user is None:
        await _reject(websocket)
        return None

    return user["user_id"]


async def _receive_first_frame(websocket: WebSocket):
    """Read one inbound frame, or ``None`` if nothing arrives in time.

    Without this a socket that never authenticates would sit open forever.
    """

    try:
        return await asyncio.wait_for(
            websocket.receive(),
            timeout=FIRST_FRAME_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None


async def _reject(websocket: WebSocket):
    """Close an unauthenticated upgrade with a private close code.

    4401 is in the 4000-4999 application range, so it reaches the client as
    ``ConnectionClosedError.code == 4401`` instead of a generic protocol
    failure. The socket must be accepted before it can be closed with a code.
    """

    try:
        await websocket.accept()
    except Exception:
        return

    try:
        await websocket.close(code=4401, reason="Unauthorized")
    except Exception:
        pass
