# Meeting Assistant

A universal desktop meeting assistant that captures microphone and system audio locally, transcribes conversations using Whisper, maintains a rolling meeting context, and provides AI-powered response suggestions through a floating Electron interface.

## Features

* 🎤 Microphone audio capture
* 🔊 Windows system-audio capture using WASAPI Loopback
* 📝 Local speech-to-text using Faster-Whisper
* 🔄 60-second audio chunks with overlap for context continuity
* 🧹 Transcript overlap deduplication
* 💾 Persistent TXT/JSON transcripts
* 🧠 Rolling 5-minute meeting context
* 🤖 LLM-powered meeting assistance
* 🚨 Manual **Help Me** trigger
* ⌨️ `Ctrl + Space` shortcut
* 🖥️ Floating Electron UI
* 📋 Copy and regenerate suggestions
* 🔒 Raw audio and transcription remain local

## Architecture

```text
Microphone ───────┐
                  │
System Audio ─────┤
                  ↓
             Python Backend
                  ↓
            Faster-Whisper
                  ↓
              Transcript
                  ↓
          Rolling 5-min Context
                  ↓
            User: Help Me
                  ↓
              Groq LLM
                  ↓
          Structured Response
                  ↓
            Electron UI
```

## Tech Stack

### Backend

* Python
* PyAudioWPatch
* WASAPI Loopback
* Faster-Whisper
* WebSockets

### AI

* Faster-Whisper for local transcription
* Groq API for LLM inference

### Frontend

* Electron
* HTML
* CSS
* JavaScript

## Project Structure

```text
Meeting_Assistant/
│
├── app/
│   ├── main.py
│   ├── audio_capture.py
│   ├── transcriber.py
│   ├── transcript_store.py
│   ├── meeting_context.py
│   ├── llm_client.py
│   ├── ui_server.py
│   ├── recordings/
│   └── transcripts/
│
├── ui/
│   ├── main.js
│   ├── preload.js
│   ├── renderer.js
│   ├── index.html
│   ├── style.css
│   └── package.json
│
├── venv/
├── .env
├── .gitignore
└── README.md
```

## Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd Meeting_Assistant
```

### 2. Create Python environment

```bash
python -m venv venv
```

Activate it on Windows:

```powershell
venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Groq API key

Create `.env` inside `app/`:

```env
GROQ_API_KEY=your_api_key_here
```

Never commit the `.env` file.

### 5. Install Electron dependencies

```bash
cd ui
npm install
```

## Running

Start the Python backend:

```powershell
cd app
python main.py
```

Then start Electron in another terminal:

```powershell
cd ui
npm start
```

## Usage

1. Start the Python backend.
2. Select the system output device.
3. Select the microphone.
4. Start the meeting capture.
5. The application continuously records and transcribes audio locally.
6. The latest conversation is maintained in a rolling 5-minute context.
7. When you need assistance, press:

```text
Ctrl + Space
```

or click:

```text
🚨 HELP ME
```

8. The LLM analyzes the recent meeting context and generates a concise suggested response.
9. Use **Copy** to copy the response or **Regenerate** to request another response.

## Privacy

Audio capture and transcription are performed locally on the user's machine.

Raw microphone and system audio are not sent to the LLM.

Only the meeting context required for generating an AI response is sent to the configured LLM API.

## Current Limitations

* Windows WASAPI support is currently the primary audio-capture target.
* Local Whisper performance depends on the user's CPU/GPU.
* The LLM requires an internet connection.
* System audio device changes may require restarting capture.
* The application is currently designed for local development rather than production deployment.
