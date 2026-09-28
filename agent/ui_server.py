import asyncio
import json
import threading
import websockets


class UIServer:

    def __init__(self, host="127.0.0.1", port=8765):

        self.host = host
        self.port = port

        self.clients = set()
        self.pending = []

        self.loop = None
        self.thread = None
        self._ready = threading.Event()

        self.message_callback = None

    def set_message_callback(self, callback):
        self.message_callback = callback

    async def _handler(self, websocket):

        self.clients.add(websocket)
        print("Electron UI connected.")

        if self.message_callback:
            try:
                self.message_callback({"type": "UI_CONNECTED"})
            except Exception as e:
                print(f"UI connect callback error: {e}")

        if self.pending:
            queued = list(self.pending)
            self.pending.clear()
            for message in queued:
                try:
                    await websocket.send(message)
                except Exception as e:
                    print(f"Failed to flush queued UI message: {e}")
                    break

        try:
            async for message in websocket:
                print("UI message:", message)

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    print("Invalid UI message:", message)
                    continue

                if self.message_callback:
                    try:
                        self.message_callback(data)
                    except Exception as e:
                        print(f"UI message callback error: {e}")

        except websockets.exceptions.ConnectionClosed:
            pass

        finally:
            self.clients.discard(websocket)
            print("Electron UI disconnected.")

    async def _start(self):

        self.loop = asyncio.get_running_loop()
        self._ready.set()

        async with websockets.serve(
            self._handler,
            self.host,
            self.port,
        ):
            print(
                f"UI WebSocket server running "
                f"on ws://{self.host}:{self.port}"
            )
            await asyncio.Future()

    def start(self):

        self.thread = threading.Thread(
            target=self._run,
            name="UIServer",
            daemon=True,
        )
        self.thread.start()
        self._ready.wait(timeout=5)

    def _run(self):
        asyncio.run(self._start())

    def send(self, message):

        if isinstance(message, dict):
            payload = json.dumps(message)
        else:
            payload = message

        if not self.loop:
            print("UI server not ready — queuing message.")
            self.pending.append(payload)
            return

        if not self.clients:
            print("Electron UI not connected — queuing message.")
            self.pending.append(payload)
            if len(self.pending) > 20:
                self.pending = self.pending[-20:]
            return

        asyncio.run_coroutine_threadsafe(
            self._broadcast(payload),
            self.loop,
        )

    async def _broadcast(self, message):

        dead_clients = set()

        for client in list(self.clients):
            try:
                await client.send(message)
            except Exception as e:
                print(f"Failed to send UI message: {e}")
                dead_clients.add(client)

        self.clients.difference_update(dead_clients)
