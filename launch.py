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
    python launch.py                     # defaults: port 3000, model llama3.2
    python launch.py --port 8080
    python launch.py --model mistral
    python launch.py --no-browser        # start server without opening browser
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


# ── ANSI colours (disabled on Windows unless using Windows Terminal) ──────────

def _supports_colour():
    if platform.system() == "Windows":
        return os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")
    return sys.stdout.isatty()

USE_COLOUR = _supports_colour()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text

def ok(msg):    print(_c("32", f"  ✓  {msg}"))
def info(msg):  print(_c("36", f"  →  {msg}"))
def warn(msg):  print(_c("33", f"  ⚠  {msg}"))
def err(msg):   print(_c("31", f"  ✕  {msg}"))
def bold(msg):  print(_c("1",  msg))
def dim(msg):   print(_c("2",  msg))


# ── Config ────────────────────────────────────────────────────────────────────

OLLAMA_API       = "http://localhost:11434"
DEFAULT_PORT     = 3000
DEFAULT_MODEL    = "llama3.2"
HTML_FILE        = "meeting_debrief.html"
STARTUP_TIMEOUT  = 30   # seconds to wait for Ollama to be ready
PULL_CHUNK_EVERY = 3    # seconds between pull progress updates


# ── Ollama: locate ────────────────────────────────────────────────────────────

def find_ollama() -> str | None:
    """
    Return the path to the Ollama executable, or None if not found.
    Checks PATH first, then well-known install locations per platform.
    """
    # Check PATH
    found = shutil.which("ollama")
    if found:
        return found

    system = platform.system()
    candidates = []

    if system == "Darwin":          # macOS
        candidates = [
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            Path.home() / ".ollama" / "ollama",
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            Path(local) / "Programs" / "Ollama" / "ollama.exe",
            Path(local) / "Ollama" / "ollama.exe",
            r"C:\Program Files\Ollama\ollama.exe",
        ]
    else:                           # Linux
        candidates = [
            "/usr/bin/ollama",
            "/usr/local/bin/ollama",
            Path.home() / ".local" / "bin" / "ollama",
        ]

    for path in candidates:
        if Path(path).is_file():
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

_ollama_proc: subprocess.Popen | None = None
_ollama_we_started = False


def start_ollama(ollama_path: str) -> bool:
    """
    Launch `ollama serve` with CORS wide-open.
    Returns True if started successfully, False on failure.
    """
    global _ollama_proc, _ollama_we_started

    env = os.environ.copy()
    env["OLLAMA_ORIGINS"] = "*"     # allow any browser origin

    try:
        # On Windows, CREATE_NO_WINDOW suppresses the console pop-up
        kwargs = {}
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
    except Exception as e:
        err(f"Could not start Ollama: {e}")
        return False


def wait_for_ollama(timeout: int = STARTUP_TIMEOUT) -> bool:
    """Poll until Ollama responds or timeout."""
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if is_ollama_running():
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

def list_models() -> list[str]:
    """Return names of locally installed Ollama models."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def pull_model(ollama_path: str, model: str) -> bool:
    """
    Pull a model, printing progress to stdout.
    Returns True on success.
    """
    info(f"Pulling model '{model}' — this only happens once…")
    info("Download size varies: llama3.2 ≈ 2 GB, mistral ≈ 4 GB")

    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            [ollama_path, "pull", model],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        last_print = 0
        for line in proc.stdout:
            line = line.strip()
            # Throttle output — Ollama prints a lot of progress lines
            now = time.time()
            if line and now - last_print > PULL_CHUNK_EVERY:
                dim(f"     {line}")
                last_print = now
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        err(f"Pull failed: {e}")
        return False


# ── HTTP server ───────────────────────────────────────────────────────────────

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files silently — no per-request log spam."""

    def log_message(self, fmt, *args):
        pass  # suppress request logs

    def end_headers(self):
        # Required headers so ES modules and Whisper WASM load correctly
        self.send_header("Cross-Origin-Opener-Policy",   "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


def find_free_port(preferred: int) -> int:
    """Use preferred port if free, otherwise find a random free one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", preferred)) != 0:
            return preferred
    # Port in use — pick a random free one
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_server: http.server.HTTPServer | None = None


def start_http_server(port: int, directory: Path) -> int:
    """
    Start a background HTTP server serving `directory`.
    Returns the port actually used.
    """
    global _server
    port  = find_free_port(port)
    os.chdir(directory)
    _server = http.server.HTTPServer(("", port), _QuietHandler)
    thread  = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    return port


def stop_http_server():
    if _server:
        _server.shutdown()


# ── Shutdown ──────────────────────────────────────────────────────────────────

def shutdown(sig=None, frame=None):
    print()
    info("Shutting down…")
    stop_http_server()
    stop_ollama()
    ok("Goodbye.")
    sys.exit(0)


signal.signal(signal.SIGINT,  shutdown)
signal.signal(signal.SIGTERM, shutdown)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Meeting Debrief Assistant launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port",       type=int, default=DEFAULT_PORT,  help=f"HTTP server port (default: {DEFAULT_PORT})")
    parser.add_argument("--model",      type=str, default=DEFAULT_MODEL, help=f"Ollama model to ensure is pulled (default: {DEFAULT_MODEL})")
    parser.add_argument("--no-browser", action="store_true",             help="Don't open browser automatically")
    args = parser.parse_args()

    # ── Banner ──
    print()
    bold("  🗒  Meeting Debrief Assistant")
    dim("  ─────────────────────────────────────────")
    print()

    # ── Locate HTML file ──
    here     = Path(__file__).parent.resolve()
    html_path = here / HTML_FILE
    if not html_path.exists():
        err(f"'{HTML_FILE}' not found next to launch.py")
        err(f"Expected: {html_path}")
        sys.exit(1)

    # ── Locate Ollama ──
    ollama_path = find_ollama()
    if not ollama_path:
        err("Ollama not found on this machine.")
        err("Download it from: https://ollama.com/download")
        err("Then re-run this launcher.")
        sys.exit(1)
    ok(f"Ollama found: {ollama_path}")

    # ── Start Ollama (if not already running) ──
    if is_ollama_running():
        ok("Ollama is already running")
    else:
        info("Starting Ollama…")
        if not start_ollama(ollama_path):
            err("Failed to start Ollama.")
            sys.exit(1)
        if not wait_for_ollama():
            print()
            err(f"Ollama did not start within {STARTUP_TIMEOUT}s.")
            err("Try running manually: ollama serve")
            sys.exit(1)
        print()
        ok("Ollama started")

    # ── Ensure model is available ──
    models = list_models()
    model_installed = any(args.model in m for m in models)

    if model_installed:
        ok(f"Model '{args.model}' is ready")
    elif models:
        # Other models exist — use the first one rather than pulling
        first = models[0].split(":")[0]
        warn(f"'{args.model}' not found. Using '{first}' instead.")
        warn(f"To pull {args.model} later:  ollama pull {args.model}")
        args.model = first
    else:
        # No models at all — must pull
        info(f"No models found. Pulling '{args.model}'…")
        if not pull_model(ollama_path, args.model):
            err(f"Failed to pull '{args.model}'.")
            err(f"Try manually: ollama pull {args.model}")
            sys.exit(1)
        ok(f"Model '{args.model}' ready")

    # ── Start HTTP server ──
    port = start_http_server(args.port, here)
    url  = f"http://localhost:{port}/{HTML_FILE}"
    ok(f"Serving on {url}")

    # ── Open browser ──
    if not args.no_browser:
        # Short delay so server is fully ready before browser hits it
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        ok("Opening browser…")

    # ── Ready ──
    print()
    bold("  ✦  Ready! Press Ctrl+C to stop.")
    dim(f"     URL:   {url}")
    dim(f"     Model: {args.model}")
    print()

    # ── Keep alive ──
    try:
        while True:
            time.sleep(1)
            # Restart Ollama if it crashed unexpectedly
            if _ollama_we_started and _ollama_proc and _ollama_proc.poll() is not None:
                warn("Ollama stopped unexpectedly — restarting…")
                start_ollama(ollama_path)
                if wait_for_ollama(timeout=15):
                    ok("Ollama restarted")
                else:
                    err("Could not restart Ollama. Refresh the browser and try again.")
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()