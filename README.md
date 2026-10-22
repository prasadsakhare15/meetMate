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

## How it works

Transcript latency is dominated by *when* the agent decides audio is finished —
not by how fast Whisper is. Two segmentation modes ship behind
`SEGMENTATION_MODE`:

**`chunk` (default, "before")** — fixed 30 s windows with 5 s overlap.
A question can be asked at second 0 of a window and waits the full window
before *any* text is sent; Whisper then decodes 35 s of audio in one pass.

```text
mic ──► 30 s window (+5 s overlap) ──► Whisper (beam 5, VAD filter, dedup)
                                          └─► SEGMENTS to cloud
                                            (~20 s average wait)
```

**`vad` ("after")** — 30 ms frames are scored by Silero VAD (bundled with
faster-whisper; no extra dependency) and closed into utterance-sized
segments: 250 ms pre-roll, close after 600 ms of silence (≥250 ms post-pad),
12 s maximum length with a 2 s lookahead for the quietest cut point and 1 s of
overlap carried into the next segment.

```text
mic ──► 30 ms frames ──► Silero VAD ──► segment ──► priority queue
                              │            │             │  (OTHERS first)
                              │            │             ▼
                              │            │        Whisper (beam 1,
                              │            │        same-channel prompt)
                              │            │             │
                              └────────────┴─────────────┴─► SEGMENTS to
                                                             cloud, per
                                                             segment
```

Each segment is transcribed and sent **as soon as it closes**, so the wait is
"until the speaker pauses" instead of "until the 30 s boundary". The server
then merges ~1 s of following speech before generating a suggestion, so a
question and its follow-up reach the LLM in one prompt.

At end of meeting the still-open segment never sees the silence that would
close it, so the agent flushes it explicitly and waits for that transcript
(plus any queued segments, up to 15 s) **before** ending the meeting — the last
words of the meeting are not dropped, and nothing is sent after `END_MEETING`.

Similarly, on **HELP ME** (`Ctrl + Space`), the agent explicitly cuts the
active speech segment without waiting for the 600 ms silence pause, transcribes
it immediately, and drains it to the cloud **before** triggering LLM inference.
This guarantees that questions asked milliseconds before clicking are fully captured
in the model prompt.

### Measured before/after

`agent/verify_pipeline.py` runs one recording through both modes with the real
production code and the real Whisper model (`base.en`, int8, 12-core CPU), and
reports the numbers below. Source: 130 s of real meeting speech.

| Question end → text ready at cloud | `chunk` (before) | `vad` (after) |
|---|---|---|
| structural wait (mean) | 17.5 s | **3.6 s** |
| + Whisper + send (mean) | 3.9 s | **0.9 s** |
| **= transcript latency (mean)** | **21.4 s** | **4.5 s** |
| transcript latency (worst) | 38.9 s | **8.2 s** |
| Whisper real-time factor | 0.111 | 0.182 |
| transcript words retained | 100% (reference) | 92.2% |

The 92.2% is **decode rendering, not lost speech**: with identical decode
settings on both paths, all 20 differing words fall *inside* audio the VAD
segmenter had already cut (`et cetera` ↔ `etc`, `tie` ↔ `thai`) — 0 words fell
outside a segment's audio span.

VAD mode's RTF is the higher of the two (0.182 vs 0.111) because beam-1
decoding costs more per second of audio than the beam-5 pass with its built-in
VAD filter over a mixed 35 s window. That still wins comfortably: segments are
seconds long rather than half a minute, so the wait to transcribe one drops
from 3.9 s to 0.9 s — far more than the RTF penalty gives back.

The two structural rows are deterministic (they come from the segmenter and
chunk geometry). The Whisper rows depend on the machine and drift ±20 % run to
run — across three verification runs chunk measured 21.4–22.2 s mean and vad
4.5–4.7 s mean.

### Verifying

```powershell
cd agent
venv\Scripts\activate
python test_pipeline.py      # fast asserts (no Whisper/ONNX), ~2 s (13 tests)
python verify_pipeline.py    # full both-modes measurement, ~2 min
```

`verify_pipeline.py` runs both modes over a test WAV (default:
`recordings/system_*.wav`, which contain meeting speech) and prints per-stage
latency, RTF, a word-level diff and a PASS/WARN/FAIL verdict (`--limit 60` for
a quick run, `--model small` for the production model). Exit code is `1` only
on FAIL.

## Project structure

```text
Meeting_Assistant/
├── agent/               # Local Python — run on user's machine
│   ├── main.py
│   ├── audio_capture.py
│   ├── auth_client.py
│   ├── segmenter.py
│   ├── segment_worker.py
│   ├── transcriber.py
│   ├── transcription_worker.py
│   ├── test_pipeline.py
│   ├── verify_pipeline.py
│   ├── cloud_client.py
│   ├── ui_server.py
│   ├── dedup.py
│   ├── timing_log.py
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
    ├── users.py
    ├── session.py
    ├── meeting_context.py
    ├── conversation_processor.py
    ├── llm_client.py
    ├── transcript_store.py
    ├── Dockerfile
    ├── .dockerignore
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

#### Local Python
```powershell
cd server
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Set GROQ_API_KEY in .env
python main.py
```

#### Docker
```powershell
cd server
# Ensure .env is populated with GROQ_API_KEY
docker build -t meeting-assistant-server .
docker run -d -p 8080:8080 --env-file .env --name meeting-server meeting-assistant-server
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
* VAD segmentation (Silero on onnxruntime) behind `SEGMENTATION_MODE=vad` — 30 ms frames become utterance-sized segments for lower suggestion latency (Phases 2-3)
* Utterance-level transcription drops question-end → suggestion latency from ~21 s to ~4.5 s mean (~8 s worst) — see [How it works](#how-it-works)
* Cloud meeting context with rolling summaries
* Meetings are resumable — a dropped connection or a cloud restart continues the same meeting
* Accounts with JWT auth — sign in, create an account, and update passwords directly in the desktop UI
* Auto-suggest ~1 s after an OTHERS question (merges immediately-following speech; 45 s cooldown — a clearly new question overrides it; HELP ME always answers immediately)
* HELP ME immediate audio cut & drain — pressing HELP ME (`Ctrl + Space`) cuts the open speech buffer, transcribes it, and drains it to the cloud so questions asked right before clicking are never missed
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
| `MEETING_TTL_SECONDS` | How long an idle meeting stays resumable (default `21600`) |
| `AUTO_SUGGEST_COOLDOWN` | Seconds between auto-suggestions (default `45`) |
| `AUTO_SUGGEST_WAIT_S` | Merge window before an auto-suggest fires (default `1`) |
| `AUTO_SUGGEST_MAX_WAIT_S` | Cap for extending the window while OTHERS keeps talking (default `3`) |
| `AUTO_SUGGEST_MIN_INTERVAL_S` | Burst guard before a new question may override the cooldown (default `5`) |
| `SECRET_KEY` | JWT signing key. If unset, one is generated and stored in `server/.secret_key` |
| `DATABASE_URL` | Optional. Set to a Postgres URL to store users there instead of `users.db` |

### `agent/.env`

| Variable | Description |
|---|---|
| `CLOUD_WS_URL` | Cloud WebSocket URL |
| `WHISPER_MODEL` | Model size (default `small`) |
| `WHISPER_DEVICE` | `cpu` or `cuda` |
| `WHISPER_COMPUTE` | Compute type (default `int8`) |
| `WHISPER_CPU_THREADS` | Whisper CPU threads (default: cores − 2, leaving 1-2 free) |
| `SEGMENTATION_MODE` | `chunk` (fixed 30 s chunks) or `vad` (30 ms frames + Silero segments, transcribed as they close) — default `chunk` |
| `VAD_THRESHOLD` | Speech probability to start/continue a segment (default `0.5`) |
| `VAD_SILENCE_MS` | Silence that closes a segment (default `600`) |
| `VAD_PRE_PAD_MS` | Audio kept before detected speech start (default `250`) |
| `VAD_POST_PAD_MS` | Audio kept after the last speech frame (default `250`) |
| `VAD_MAX_SEGMENT_S` | Max segment length before a forced cut (default `12`) |
| `VAD_CUT_LOOKAHEAD_S` | Window searched for the lowest-energy cut point (default `2`) |
| `VAD_OVERLAP_S` | Audio carried into the next segment after a forced cut (default `1`) |
| `SILERO_VAD_PATH` | Optional dir/file override for the Silero ONNX models (default: faster-whisper's bundled assets) |
| `TIMING_LOG` | `1`/`0` — stage timing logs (default `1`) |
| `TIMING_LOG_DIR` | Where timing JSONL/summary files go (default `agent/timings/`) |

## Current limitations

- Windows WASAPI is the primary audio capture target (cross-platform capture planned for future phases)
- Cloud server is designed for self-hosting (direct Python or Docker container)
