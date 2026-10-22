import asyncio
import json
import os
import threading
from collections import deque
from pathlib import Path

import websockets
from dotenv import load_dotenv

from timing_log import TIMING


load_dotenv(Path(__file__).resolve().parent / ".env")

# Roughly 4 hours of one SEGMENTS message per 30s chunk.
MAX_PENDING_MESSAGES = 500


class CloudClient:

    def __init__(
        self,
        url=None,
        reconnect_delay=3,
    ):

        self.url = url or os.getenv(
            "CLOUD_WS_URL",
            "ws://127.0.0.1:8080/ws",
        )

        self.reconnect_delay = reconnect_delay
        self.message_callback = None

        # JWT from the cloud's /auth/login — sent as a Bearer header on every
        # (re)connect. None until the desktop UI signs in.
        self.token = None
        self._auth_failed = False

        self.loop = None
        self.thread = None
        self.ws = None
        self.connected = False
        self._stop = threading.Event()

        # The current meeting. Kept here (not only sent) so a connection
        # that drops mid-meeting can reopen the same meeting on reconnect.
        self.meeting_id = None

        self._pending = deque()
        self._buffer_lock = threading.Lock()

    def set_message_callback(self, callback):
        self.message_callback = callback

    def set_token(self, token):
        """Store the auth token and reconnect so it takes effect right away.

        Called when the desktop UI signs in — including while the connect
        loop is stuck waiting after a rejected token.
        """

        self.token = token
        self._auth_failed = False

        if self.loop and self.ws:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop)

    def start(self):

        self._stop.clear()

        self.thread = threading.Thread(
            target=self._run,
            name="CloudClient",
            daemon=True,
        )

        self.thread.start()

    def stop(self):

        self._stop.set()

        if self.loop and self.ws:
            future = asyncio.run_coroutine_threadsafe(
                self._resume(),
                self.loop,
            )

            try:
                future.result(timeout=2)
            except Exception:
                pass

            asyncio.run_coroutine_threadsafe(
                self._close(),
                self.loop,
            )

    async def _close(self):

        if self.ws:
            await self.ws.close()

    # =================================================
    # MEETING LIFECYCLE
    # =================================================

    def start_meeting(self, meeting_id):
        """Announce a meeting. Idempotent — safe to repeat on reconnect."""

        self.meeting_id = meeting_id

        self.send({
            "type": "START_MEETING",
            "meeting_id": meeting_id,
        })

    def end_meeting(self):

        meeting_id = self.meeting_id

        if not meeting_id:
            return

        # Send first, then forget: the frame still carries its own meeting
        # id even if it has to wait in the buffer.
        self.send({
            "type": "END_MEETING",
            "meeting_id": meeting_id,
        })

        self.meeting_id = None

    def send_segments(self, segments, key=None):

        # Phase 0 timing: whether the frame went out now or sits in the
        # offline buffer.
        TIMING.event(
            "cloud_sent",
            key=key,
            delivered=bool(self.connected),
        )

        self.send({
            "type": "SEGMENTS",
            "meeting_id": self.meeting_id,
            "segments": segments,
        })

    def send_help_me(self):

        self.send({
            "type": "HELP_ME",
            "meeting_id": self.meeting_id,
        })

    # =================================================
    # SENDING
    # =================================================

    def send(self, message: dict):

        with self._buffer_lock:

            if not self.connected:
                self._buffer(message)
                return

        asyncio.run_coroutine_threadsafe(
            self._send(message),
            self.loop,
        )

    def _buffer(self, message):
        """Queue while offline so a reconnect does not lose transcript."""

        if len(self._pending) >= MAX_PENDING_MESSAGES:
            dropped = self._pending.popleft()
            print(
                "Cloud buffer full — dropped oldest "
                f"{dropped.get('type')} message."
            )

        self._pending.append(message)

        print(
            f"Cloud offline — buffered {message.get('type')} "
            f"({len(self._pending)} pending)."
        )

    async def _send(self, message: dict):

        if self.ws:
            await self.ws.send(json.dumps(message))

    # =================================================
    # CONNECTION
    # =================================================

    def _run(self):

        asyncio.run(self._connect_loop())

    async def _connect_loop(self):

        self.loop = asyncio.get_running_loop()

        while not self._stop.is_set():

            try:
                print(f"Connecting to cloud backend at {self.url}...")

                headers = (
                    {"Authorization": f"Bearer {self.token}"}
                    if self.token
                    else None
                )

                async with websockets.connect(
                    self.url,
                    additional_headers=headers,
                ) as ws:

                    self.ws = ws

                    # Hold off direct sends until _resume() has reopened
                    # the meeting, so buffered segments keep their order.
                    with self._buffer_lock:
                        self.connected = False

                    print("Connected to cloud backend.")

                    await self._resume()

                    if self.message_callback:
                        self.message_callback({
                            "type": "CLOUD_STATUS",
                            "text": "connected",
                        })

                    async for raw in ws:

                        try:
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8")
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            print("Invalid cloud message:", raw)
                            continue

                        if self.message_callback:
                            try:
                                self.message_callback(message)
                            except Exception as callback_error:
                                print(
                                    "Cloud message callback error:",
                                    callback_error,
                                )

            except websockets.exceptions.ConnectionClosed as e:

                close = getattr(e, "rcvd", None)

                if close is not None and getattr(close, "code", None) == 4401:
                    print("Cloud rejected the credentials (close 4401).")
                    self._auth_failed = True

                    if self.message_callback:
                        self.message_callback({
                            "type": "CLOUD_STATUS",
                            "text": "unauthorized",
                        })
                else:
                    print(f"Cloud connection lost: {e}")

            except Exception as e:
                print(f"Cloud connection lost: {e}")

            finally:

                with self._buffer_lock:
                    self.connected = False

                self.ws = None

                if self.message_callback and not self._stop.is_set():
                    self.message_callback({
                        "type": "CLOUD_STATUS",
                        "text": "disconnected",
                    })

            if self._stop.is_set():
                break

            if self._auth_failed:
                # Do not hammer the server with the same bad token. Wait
                # here — set_token() flips the flag and we reconnect at once.
                print("Waiting for new credentials before reconnecting...")

                while self._auth_failed and not self._stop.is_set():
                    await asyncio.sleep(0.5)

                continue

            print(f"Retrying in {self.reconnect_delay}s...")

            await asyncio.sleep(self.reconnect_delay)

    async def _resume(self):
        """Reopen the active meeting, then flush whatever was buffered."""

        # 1. Reopen the meeting first, so buffered segments land in it
        #    instead of being rejected as "no active meeting".
        if self.meeting_id:
            print(f"Resuming meeting {self.meeting_id} on cloud.")
            await self._send({
                "type": "START_MEETING",
                "meeting_id": self.meeting_id,
            })

        # 2. Drain in order. Every frame carries its own meeting_id, so
        #    frames for an already-finished meeting still land in that one.
        flushed = 0

        while True:

            with self._buffer_lock:

                pending = list(self._pending)
                self._pending.clear()

                if not pending:
                    self.connected = True
                    break

            for message in pending:
                await self._send(message)
                flushed += 1

        if flushed:
            print(f"Flushed {flushed} buffered message(s) to cloud.")
