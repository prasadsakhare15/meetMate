import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from session import MeetingSession


load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(
    title="Meeting Assistant Cloud",
    description="Cloud backend for meeting context, transcripts, and LLM suggestions.",
    version="2.0.0",
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):

    await websocket.accept()

    session = MeetingSession(websocket)

    try:
        await session.run()
    except WebSocketDisconnect:
        pass


def main():

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))

    print(f"Meeting Assistant cloud server on ws://{host}:{port}/ws")

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
