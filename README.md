# Meeting Assistant

A desktop meeting copilot with a **local agent** (audio capture, Whisper transcription), a **desktop UI** (Electron), and a **cloud server** (meeting context, transcripts, LLM suggestions).

Raw audio never leaves the user's machine. Only transcribed text is sent to the cloud.

## Architecture

```text
┌──────────────────────── LOCAL (user's machine) ────────────────────────┐
│                                                                        │
│  Microphone ──┐                                                        │
│               ├──► agent/ (Python) ──► Faster-Whisper ──► transcript   │
│  System Audio ┘         │                                              │
│                         │ WebSocket (transcript text only)             │
│                         ▼                                              │
│              desktop/ (Electron) ◄── WebSocket ──► agent/              │
│                                                                        │
└───────────────────────────────┬────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────── CLOUD ─────────────────────────────────────────┐
│                                                                        │
│  server/ — FastAPI WebSocket                                           │
│       ├── Meeting context (rolling 5-min window + summaries)         │
│       ├── Conversation processing                                      │
│       ├── Transcript persistence                                       │
│       └── Groq LLM ──► structured suggestions                          │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

## Project structure

```text
Meeting_Assistant/
├── agent/               # Local Python — run on user's machine
│   ├── main.py
│   ├── audio_capture.py
│   ├── transcriber.py
│   ├── transcription_worker.py
│   ├── cloud_client.py
│   ├── ui_server.py
│   ├── dedup.py
│   └── requirements.txt
│
├── desktop/             # Electron floating UI
│   ├── main.js
│   ├── renderer.js
│   ├── preload.js
│   ├── index.html
│   ├── style.css
│   └── package.json
│
└── server/              # Cloud backend — deploy separately
    ├── main.py
    ├── session.py
    ├── meeting_context.py
    ├── conversation_processor.py
    ├── llm_client.py
    ├── transcript_store.py
    └── requirements.txt
```

Each top-level folder is **one thing you run**.

| Folder | What it is | Where it runs |
|---|---|---|
| `agent/` | Capture + Whisper + bridges UI ↔ cloud | User's machine |
| `desktop/` | Floating Electron window | User's machine |
| `server/` | Context, storage, LLM | Cloud |

## Setup

### 1. Cloud server

```powershell
cd server
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Set GROQ_API_KEY in .env
python main.py
```

Default WebSocket: `ws://127.0.0.1:8080/ws`

## Running

Start the **cloud server** once, then just start the desktop app. Electron launches the local agent for you.

```powershell
# Terminal 1 — cloud (keep this running)
cd server
venv\Scripts\activate
python main.py

# Terminal 2 — desktop + local agent
cd desktop
npm install
npm start
```

In the floating window:

1. Wait until status is **LIVE** (agent connected) and Whisper has finished loading (**Ready**).
2. Pick speakers/headphones and a microphone.
3. Click **Start meeting**.
4. Press **Ctrl + Space** or **HELP ME** when you need a suggestion.
5. Click **Stop meeting** to end capture.

Electron uses `agent/venv/Scripts/python.exe` if that venv exists. If the agent is already running by itself, set `ELECTRON_SKIP_AGENT=1` before `npm start`.

### Manual agent (optional)

```powershell
cd agent
venv\Scripts\activate
python main.py
```

## Features

* Local audio capture and Whisper transcription
* Cloud meeting context with rolling summaries
* Auto-suggest when OTHERS ask a question (45s cooldown)
* UI status: Listening / Thinking / Error
* Device selection and Start/Stop in the floating UI
* Electron can launch the local agent on startup

## Privacy

- Raw audio stays on the user's machine
- Transcription runs locally with Faster-Whisper
- Only transcribed text is sent to the cloud
- Cloud sends text context to Groq for LLM inference

## Environment variables

### `server/.env`

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | Groq API key |
| `HOST` | Bind address (default `0.0.0.0`) |
| `PORT` | Port (default `8080`) |

### `agent/.env`

| Variable | Description |
|---|---|
| `CLOUD_WS_URL` | Cloud WebSocket URL |
| `WHISPER_MODEL` | Model size (default `small`) |
| `WHISPER_DEVICE` | `cpu` or `cuda` |
| `WHISPER_COMPUTE` | Compute type (default `int8`) |

## Current limitations

- Windows WASAPI is the primary audio capture target
- Cloud WebSocket has no authentication yet (add before public deployment)
- The cloud server still runs as a separate process
