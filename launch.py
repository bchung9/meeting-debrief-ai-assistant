#!/usr/bin/env python3
"""
Meeting Debrief Assistant — Launcher
======================================
Handles everything so users don't have to:
  1. Locates Ollama across Mac / Windows / Linux
  2. Starts Ollama with CORS enabled (if not already running)
  3. Pulls a default model if none are installed
  4. Serves the HTML frontend over HTTP (required for ES modules + Whisper)
  5. Opens the browser automatically

Usage:
    python launch.py                     # start everything
    python launch.py --port 8080         # use a different port
    python launch.py --model mistral     # ensure a specific model is pulled
    python launch.py --no-browser        # start server without opening browser
    python launch.py --check             # verify setup only, don't start

Requirements:
    Python 3.8+  (no third-party packages needed)
    Ollama       https://ollama.com/download
"""

import argparse
import http.server
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

# Require Python 3.8+
if sys.version_info < (3, 8):
    print("Python 3.8 or later is required. Download it from https://python.org")
    sys.exit(1)


# ── ANSI colours ──────────────────────────────────────────────────────────────

def _supports_colour() -> bool:
    if platform.system() == "Windows":
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
    return sys.stdout.isatty()

USE_COLOUR = _supports_colour()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text

def ok(msg: str):    print(_c("32", f"  ✓  {msg}"))
def info(msg: str):  print(_c("36", f"  →  {msg}"))
def warn(msg: str):  print(_c("33", f"  ⚠  {msg}"))
def err(msg: str):   print(_c("31", f"  ✕  {msg}"))
def bold(msg: str):  print(_c("1",  msg))
def dim(msg: str):   print(_c("2",  msg))
def blank():         print()


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_API       = "http://localhost:11434"
DEFAULT_PORT     = 3000
DEFAULT_MODEL    = "llama3.2"
HTML_FILE        = "meeting_debrief.html"
STARTUP_TIMEOUT  = 30    # seconds to wait for Ollama to become ready
PULL_CHUNK_SECS  = 3     # seconds between pull progress prints


# ── Ollama: locate ────────────────────────────────────────────────────────────

def find_ollama() -> Optional[str]:
    """
    Return the path to the Ollama executable, or None if not found.
    Checks PATH first, then well-known install locations per platform.
    """
    found = shutil.which("ollama")
    if found:
        return found

    system = platform.system()
    candidates: List[Path] = []

    if system == "Darwin":
        candidates = [
            Path("/usr/local/bin/ollama"),
            Path("/opt/homebrew/bin/ollama"),
            Path.home() / ".ollama" / "ollama",
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(local) / "Programs" / "Ollama" / "ollama.exe",
            Path(local) / "Ollama" / "ollama.exe",
            Path(r"C:\Program Files\Ollama\ollama.exe"),
        ]
    else:  # Linux
        candidates = [
            Path("/usr/bin/ollama"),
            Path("/usr/local/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama",
        ]

    for path in candidates:
        if path.is_file():
            return str(path)

    return None


# ── Ollama: running? ──────────────────────────────────────────────────────────

def is_ollama_running() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ── Ollama: start ─────────────────────────────────────────────────────────────

_ollama_proc: Optional[subprocess.Popen] = None
_ollama_we_started: bool = False


def start_ollama(ollama_path: str) -> bool:
    """
    Launch `ollama serve` with CORS wide-open.
    Returns True if the process started, False on error.
    """
    global _ollama_proc, _ollama_we_started

    env = os.environ.copy()
    env["OLLAMA_ORIGINS"] = "*"

    try:
        kwargs: Dict = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        _ollama_proc = subprocess.Popen(
            [ollama_path, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        _ollama_we_started = True
        return True
    except Exception as exc:
        err(f"Could not start Ollama: {exc}")
        return False


def wait_for_ollama(timeout: int = STARTUP_TIMEOUT) -> bool:
    """Poll until Ollama responds or the timeout expires."""
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if is_ollama_running():
            print()   # end the dots line
            return True
        sys.stdout.write(f"\r  →  Waiting for Ollama{'.' * (dots % 4)}   ")
        sys.stdout.flush()
        dots += 1
        time.sleep(0.5)
    print()
    return False


def stop_ollama():
    """Terminate Ollama only if we started it."""
    if _ollama_we_started and _ollama_proc and _ollama_proc.poll() is None:
        _ollama_proc.terminate()
        try:
            _ollama_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _ollama_proc.kill()


# ── Ollama: models ────────────────────────────────────────────────────────────

def list_models() -> List[str]:
    """Return names of locally installed Ollama models."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def pull_model(ollama_path: str, model: str) -> bool:
    """
    Pull a model, printing periodic progress to stdout.
    Returns True on success.
    """
    info(f"Pulling model '{model}' — this only happens once, then it's cached.")
    info("Typical sizes: llama3.2 ≈ 2 GB · mistral ≈ 4 GB · phi4 ≈ 9 GB")
    blank()

    env = os.environ.copy()
    last_print = 0.0

    try:
        proc = subprocess.Popen(
            [ollama_path, "pull", model],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.strip()
            now = time.time()
            if line and now - last_print > PULL_CHUNK_SECS:
                dim(f"     {line}")
                last_print = now
        proc.wait()

        if proc.returncode != 0:
            err(f"Pull exited with code {proc.returncode}.")
            return False
        return True

    except Exception as exc:
        err(f"Pull failed: {exc}")
        return False


# ── HTTP server ───────────────────────────────────────────────────────────────

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files without printing a log line for every request."""

    def log_message(self, fmt: str, *args) -> None:
        pass  # silence per-request logs

    def end_headers(self) -> None:
        # Required so ES modules (Transformers.js, Whisper WASM) load correctly
        self.send_header("Cross-Origin-Opener-Policy",   "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


def find_free_port(preferred: int) -> int:
    """Use the preferred port if it's free; otherwise find any free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", preferred)) != 0:
            return preferred
    # Preferred port is taken — ask the OS for a free one
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_server: Optional[http.server.HTTPServer] = None


def start_http_server(port: int, directory: Path) -> int:
    """
    Start a background HTTP server serving `directory`.
    Returns the port actually used.
    """
    global _server
    port = find_free_port(port)
    os.chdir(directory)
    _server = http.server.HTTPServer(("", port), _QuietHandler)
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    return port


def stop_http_server():
    if _server:
        _server.shutdown()


# ── Shutdown ──────────────────────────────────────────────────────────────────

def shutdown(sig=None, frame=None):
    blank()
    info("Shutting down…")
    stop_http_server()
    stop_ollama()
    ok("Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)


# ── Check mode ────────────────────────────────────────────────────────────────

def run_check(ollama_path: Optional[str]) -> bool:
    """
    Verify the environment without starting anything.
    Returns True if everything looks good.
    """
    bold("  Checking environment…")
    blank()
    all_good = True

    # Python version
    pv = sys.version_info
    ok(f"Python {pv.major}.{pv.minor}.{pv.micro}")

    # HTML file
    here      = Path(__file__).parent.resolve()
    html_path = here / HTML_FILE
    if html_path.exists():
        ok(f"{HTML_FILE} found")
    else:
        err(f"{HTML_FILE} not found (expected alongside launch.py)")
        all_good = False

    # Ollama binary
    if ollama_path:
        ok(f"Ollama binary: {ollama_path}")
    else:
        err("Ollama not found — download from https://ollama.com/download")
        all_good = False

    # Ollama running
    if is_ollama_running():
        ok("Ollama is running")
        models = list_models()
        if models:
            ok(f"Models installed: {', '.join(m.split(':')[0] for m in models)}")
        else:
            warn("No models installed yet — the launcher will pull one on first run")
    else:
        warn("Ollama is not currently running (launcher will start it automatically)")

    blank()
    if all_good:
        ok("Everything looks good — run launch.py to start.")
    else:
        err("Some issues need fixing before the app will work.")

    return all_good


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Meeting Debrief Assistant — launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port",       type=int, default=DEFAULT_PORT,
                        help=f"HTTP server port (default: {DEFAULT_PORT})")
    parser.add_argument("--model",      type=str, default=DEFAULT_MODEL,
                        help=f"Ollama model to ensure is pulled (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Start server without opening a browser tab")
    parser.add_argument("--check",      action="store_true",
                        help="Verify setup only — don't start anything")
    args = parser.parse_args()

    # ── Banner ──
    blank()
    bold("  🗒  Meeting Debrief Assistant")
    dim( "  ─────────────────────────────────────────")
    blank()

    # ── Locate Ollama ──
    ollama_path = find_ollama()

    # ── Check mode ──
    if args.check:
        run_check(ollama_path)
        sys.exit(0)

    # ── Require Ollama ──
    if not ollama_path:
        err("Ollama is not installed.")
        blank()
        dim("  Download it from: https://ollama.com/download")
        dim("  Install it, then re-run this launcher.")
        sys.exit(1)
    ok(f"Ollama found: {ollama_path}")

    # ── Require HTML file ──
    here      = Path(__file__).parent.resolve()
    html_path = here / HTML_FILE
    if not html_path.exists():
        err(f"'{HTML_FILE}' not found.")
        dim(f"  Expected: {html_path}")
        dim( "  Make sure launch.py and meeting_debrief.html are in the same folder.")
        sys.exit(1)

    # ── Start Ollama (if not already running) ──
    if is_ollama_running():
        ok("Ollama is already running")
    else:
        info("Starting Ollama…")
        if not start_ollama(ollama_path):
            err("Failed to launch Ollama.")
            dim("  Try starting it manually: ollama serve")
            sys.exit(1)
        if not wait_for_ollama():
            err(f"Ollama didn't respond within {STARTUP_TIMEOUT} seconds.")
            dim("  Check that no firewall is blocking port 11434.")
            dim("  Try manually: ollama serve")
            sys.exit(1)
        ok("Ollama started")

    # ── Ensure a model is available ──
    models        = list_models()
    model_ready   = any(args.model in m for m in models)

    if model_ready:
        ok(f"Model '{args.model}' is ready")
    elif models:
        # Other models exist — use the first installed one
        first = models[0].split(":")[0]
        warn(f"'{args.model}' is not installed — using '{first}' instead.")
        dim(f"  To install {args.model} later: ollama pull {args.model}")
        args.model = first
    else:
        # No models at all — pull the default
        blank()
        if not pull_model(ollama_path, args.model):
            err(f"Could not pull '{args.model}'.")
            dim(f"  Try manually: ollama pull {args.model}")
            sys.exit(1)
        ok(f"Model '{args.model}' ready")

    # ── Start HTTP server ──
    port = start_http_server(args.port, here)
    if port != args.port:
        warn(f"Port {args.port} was in use — using port {port} instead.")
    url = f"http://localhost:{port}/{HTML_FILE}"
    ok(f"Serving: {url}")

    # ── Open browser ──
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        ok("Opening browser…")

    # ── Ready ──
    blank()
    bold("  ✦  Ready! Press Ctrl+C to stop.")
    dim( f"     URL    → {url}")
    dim( f"     Model  → {args.model}")
    dim( f"     Whisper downloads on first audio transcription")
    blank()

    # ── Keep alive + Ollama watchdog ──
    try:
        while True:
            time.sleep(2)
            if _ollama_we_started and _ollama_proc and _ollama_proc.poll() is not None:
                warn("Ollama stopped unexpectedly — restarting…")
                if start_ollama(ollama_path) and wait_for_ollama(timeout=15):
                    ok("Ollama restarted")
                else:
                    err("Could not restart Ollama.")
                    dim("  Refresh the browser and re-run launch.py if the app stops working.")
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()