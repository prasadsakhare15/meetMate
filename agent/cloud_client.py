import asyncio
import json
import os
import threading
from pathlib import Path

import websockets
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


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

        self.loop = None
        self.thread = None
        self.ws = None
        self.connected = False
        self._stop = threading.Event()

    def set_message_callback(self, callback):
        self.message_callback = callback

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
            asyncio.run_coroutine_threadsafe(
                self._close(),
                self.loop,
            )

    async def _close(self):

        if self.ws:
            await self.ws.close()

    def send(self, message: dict):

        if not self.connected or not self.loop:
            print("Cloud backend not connected — message dropped.")
            return

        asyncio.run_coroutine_threadsafe(
            self._send(message),
            self.loop,
        )

    async def _send(self, message: dict):

        if self.ws:
            await self.ws.send(json.dumps(message))

    def send_segments(self, segments):

        self.send({
            "type": "SEGMENTS",
            "segments": segments,
        })

    def send_help_me(self):

        self.send({"type": "HELP_ME"})

    def _run(self):

        asyncio.run(self._connect_loop())

    async def _connect_loop(self):

        self.loop = asyncio.get_running_loop()

        while not self._stop.is_set():

            try:
                print(f"Connecting to cloud backend at {self.url}...")

                async with websockets.connect(self.url) as ws:
                    self.ws = ws
                    self.connected = True

                    print("Connected to cloud backend.")

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

            except Exception as e:

                self.connected = False
                self.ws = None

                if self.message_callback:
                    self.message_callback({
                        "type": "CLOUD_STATUS",
                        "text": "disconnected",
                    })

                if self._stop.is_set():
                    break

                print(f"Cloud connection lost: {e}")
                print(f"Retrying in {self.reconnect_delay}s...")

                await asyncio.sleep(self.reconnect_delay)
