# Meeting Debrief AI Assistant README

A local-first meeting intelligence tool. Paste or record a transcript, get back a structured debrief — action items, decisions, open questions, and a summary — in seconds.

Everything runs on your machine. No API keys (there is a version that requires API keys). No subscriptions. No data sent to the cloud.

---

## How it works

```
Audio file  ──▶  Whisper (browser)  ──▶  Transcript
                                              │
                                              ▼
                                     Ollama (local LLM)
                                              │
                                              ▼
                                    Structured Debrief
                                    (saved to SQLite)
```

- **Speech-to-text** — Whisper runs directly in your browser via Transformers.js. Your audio never leaves your machine.
- **Language model** — Ollama runs a local LLM (Llama, Mistral, Gemma, or Phi) on your GPU or CPU.
- **History** — every debrief is saved to a local SQLite database with full-text search.
- **Launcher** — a single Python script starts Ollama, the database, the HTTP server, and opens your browser automatically.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| **Python** | 3.8 or later | Pre-installed on Mac/Linux; download from [python.org](https://python.org) on Windows |
| **Ollama** | Any | Download from [ollama.com/download](https://ollama.com/download) |
| **Browser** | Chrome 113+ or Edge 113+ | Required for Whisper audio transcription (WebAssembly) |
| **RAM** | 8 GB minimum | 16 GB recommended for larger models |
| **Disk** | 3–10 GB free | Depends on which LLM you download |

> **Firefox users:** The app works for text transcripts and analysis. Audio transcription (Whisper) requires Chrome or Edge due to WebAssembly constraints.

---

## Installation

### Step 1 — Install Ollama

Download and install Ollama for your operating system from **[ollama.com/download](https://ollama.com/download)**.

After installing, you don't need to start it manually — the launcher handles that.

### Step 2 — Download the app files

Put these three files in the same folder:

```
meeting-debrief/
├── launch.py
├── launch.sh        (Mac / Linux)
├── launch.bat       (Windows)
└── meeting_debrief.html
```

### Step 3 — Run the launcher

**Mac / Linux:**
```bash
cd meeting-debrief
chmod +x launch.sh
./launch.sh
```

Or with Python directly:
```bash
python3 launch.py
```

**Windows:**

Double-click `launch.bat`, or in a terminal:
```
python launch.py
```

The launcher will:
1. Find Ollama on your machine
2. Start it with the correct settings
3. Download `llama3.2` (~2 GB) if no models are installed yet
4. Create the `debriefs.db` database
5. Open your browser to `http://localhost:3000/meeting_debrief.html`

> The first run takes longer because it downloads the language model. Subsequent starts are fast — everything is cached.

---

## Usage

### Analysing a text transcript

1. Paste your transcript into the **Transcript** panel, or click **📂 Load .txt** to open a file
2. Click **✦ Analyse** (or press `Ctrl+Enter`)
3. The debrief streams into the right panel in real time
4. Click **💾 Save .md** to export as Markdown, or **⎘ Copy** to copy to clipboard

### Transcribing audio

1. Click **🎙 Audio** and choose an audio file (`.mp3`, `.wav`, `.m4a`, `.ogg`, `.webm`)
2. Click **⟳ Transcribe** — Whisper downloads on first use (~75–470 MB depending on model), then runs locally
3. Review the transcript that appears, edit if needed
4. Click **✦ Analyse** to generate the debrief

### Browsing history

Click **📋 History** in the toolbar to open the history sidebar. Every debrief is saved automatically after analysis.

- **Search** — type in the search box to search across all past transcripts and debriefs using full-text search
- **Load** — click any entry to reload its transcript and debrief
- **Delete** — hover an entry and click **✕** to remove it

---

## Choosing a model

Select your LLM from the **LLM** dropdown in the top-right corner. The model is pulled automatically on first use.

| Model | Size | Context | Best for |
|---|---|---|---|
| `llama3.2` | ~2 GB | 128k tokens | Default — fast and accurate |
| `llama3.1` | ~5 GB | 128k tokens | Longer, more detailed outputs |
| `gemma3` | ~3 GB | 128k tokens | Strong reasoning |
| `mistral` | ~4 GB | 32k tokens | Good quality, smaller context |
| `phi4` | ~9 GB | 16k tokens | Compact but capable |

To pre-install a model before opening the app:
```bash
ollama pull llama3.2
```

To use a different default model when launching:
```bash
python launch.py --model mistral
```

### Choosing a Whisper model (audio only)

Select from the **Whisper** dropdown. Models download once and are cached in your browser.

| Model | Size | Speed | Quality |
|---|---|---|---|
| `tiny.en` | ~75 MB | Fastest | Good for clear audio |
| `base.en` | ~145 MB | Balanced | **Default** |
| `small.en` | ~470 MB | Slower | Best accuracy |

---

## Launcher options

```
python launch.py [options]

Options:
  --port N         Run the HTTP server on port N (default: 3000)
  --model NAME     Ensure this Ollama model is installed (default: llama3.2)
  --no-browser     Start the server without opening a browser tab
  --check          Verify your setup without starting anything

Examples:
  python launch.py --port 8080
  python launch.py --model mistral
  python launch.py --check
```

---

## Files created

| File | Location | Description |
|---|---|---|
| `debriefs.db` | Same folder as `launch.py` | SQLite database — all your debrief history |

The database is created automatically on first run. To start fresh, delete `debriefs.db` and relaunch.

---

## Troubleshooting

### "Cannot reach Ollama" / "Ollama is not running"

The browser can't connect to Ollama. The launcher starts it automatically, but if you see this error mid-session it may have crashed.

**Fix:** Stop and restart the launcher (`Ctrl+C`, then `python launch.py`). The launcher includes a watchdog that restarts Ollama automatically if it crashes, but a full restart is the most reliable fix.

---

### Audio transcription doesn't start

Whisper requires Chrome 113+ or Edge 113+. Firefox does not support the WebAssembly format Whisper uses.

**Fix:** Open the app in Chrome or Edge. If you're already on Chrome, check that it's up to date (`chrome://settings/help`).

---

### Model not found / "Pull it first"

The model shown in the LLM dropdown hasn't been downloaded yet.

**Fix:** Either select a model that's already installed, or run in a terminal:
```bash
ollama pull llama3.2
```
The launcher pulls `llama3.2` automatically on first run, but won't auto-pull other models.

---

### Debrief cuts off mid-way

The transcript is too long for the selected model's context window. The token counter in the transcript panel header turns amber (warning) or red (likely to be cut off).

**Fix:** Switch to `llama3.2` or `llama3.1` (128k context window), or shorten the transcript by removing small talk and focusing on the key parts of the meeting.

---

### App opens but the page is blank / JS errors in console

The page must be served over HTTP — opening `meeting_debrief.html` directly as a file (`file://`) won't work because browsers block the module scripts the app needs.

**Fix:** Always use the launcher, which serves the file over HTTP. If you must open it manually:
```bash
python3 -m http.server 3000
# then open http://localhost:3000/meeting_debrief.html
```

---

### Port 3000 already in use

Another app is using port 3000. The launcher detects this and picks a free port automatically, printing the actual URL in the terminal.

**Fix:** Use a specific port:
```bash
python launch.py --port 8080
```

---

### Transcription produces garbled text

The audio quality may be too low, or the recording has heavy background noise.

**Fix:** Try the `small.en` Whisper model (higher accuracy), or pre-process the audio to remove noise. For phone call recordings, make sure both sides of the conversation are audible.

---

## Project structure

```
meeting-debrief/
├── launch.py              # Launcher: starts Ollama, SQLite, HTTP server + REST API
├── launch.sh              # Mac / Linux convenience wrapper
├── launch.bat             # Windows convenience wrapper
├── meeting_debrief.html   # The entire frontend (HTML + CSS + JS, single file)
├── debriefs.db            # Auto-created: SQLite history database
└── README.md              # This file
```

The REST API is served by `launch.py` on the same port as the HTML file — no separate backend process is needed.

| Endpoint | Method | Description |
|---|---|---|
| `/api/debriefs` | `GET` | List history (newest first) |
| `/api/debriefs` | `POST` | Save a new debrief |
| `/api/debriefs/{id}` | `GET` | Fetch one debrief in full |
| `/api/debriefs/{id}` | `DELETE` | Delete a debrief |
| `/api/search?q=...` | `GET` | Full-text search (SQLite FTS5) |

---

## Privacy

All processing happens on your machine:

- Transcripts are never sent to an external server
- Audio files are decoded and processed in your browser
- The LLM runs locally via Ollama
- History is stored in `debriefs.db` on your own disk
- The only network requests made are to download models on first use (from Ollama's servers and Hugging Face)

---

## Known limitations

- Audio files over 500 MB are rejected; files over 60 minutes show a warning
- Firefox does not support Whisper audio transcription
- Very long transcripts (approaching the model's context window) may produce truncated output
- The app requires the launcher to be running — it cannot be opened as a plain HTML file
