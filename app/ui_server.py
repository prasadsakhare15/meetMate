import asyncio
import json
import threading
import websockets


class UIServer:

    def __init__(self, host="127.0.0.1", port=8765):

        self.host = host
        self.port = port

        self.clients = set()

        self.loop = None
        self.thread = None

        # Callback for messages coming from Electron
        self.message_callback = None

    # --------------------------------------------------
    # Register callback
    # --------------------------------------------------

    def set_message_callback(self, callback):

        self.message_callback = callback

    # --------------------------------------------------
    # WebSocket handler
    # --------------------------------------------------

    async def _handler(self, websocket):

        self.clients.add(websocket)

        print("Electron UI connected.")

        try:

            async for message in websocket:

                print(
                    "UI message:",
                    message
                )

                try:

                    data = json.loads(message)

                except json.JSONDecodeError:

                    print(
                        "Invalid UI message:",
                        message
                    )

                    continue

                # Send message to Python application
                if self.message_callback:

                    self.message_callback(data)

        except websockets.exceptions.ConnectionClosed:

            pass

        finally:

            self.clients.discard(websocket)

            print(
                "Electron UI disconnected."
            )

    # --------------------------------------------------
    # Start server
    # --------------------------------------------------

    async def _start(self):

        self.loop = (
            asyncio.get_running_loop()
        )

        async with websockets.serve(
            self._handler,
            self.host,
            self.port
        ):

            print(
                f"UI WebSocket server running "
                f"on ws://{self.host}:{self.port}"
            )

            await asyncio.Future()

    # --------------------------------------------------
    # Start in background thread
    # --------------------------------------------------

    def start(self):

        self.thread = threading.Thread(
            target=self._run,
            daemon=True
        )

        self.thread.start()

    def _run(self):

        asyncio.run(
            self._start()
        )

    # --------------------------------------------------
    # Python → Electron
    # --------------------------------------------------

    def send(self, message):

        if not self.clients:
            return

        if isinstance(message, dict):

            message = json.dumps(message)

        asyncio.run_coroutine_threadsafe(
            self._broadcast(message),
            self.loop
        )

    async def _broadcast(self, message):

        dead_clients = set()

        for client in self.clients:

            try:

                await client.send(
                    message
                )

            except Exception:

                dead_clients.add(client)

        self.clients.difference_update(
            dead_clients
        )