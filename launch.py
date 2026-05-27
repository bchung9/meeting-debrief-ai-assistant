
"""
Meeting Debrief Assistant — Launcher
======================================
Handles everything so users don't have to:
  1. Locates Ollama across Mac / Windows / Linux
  2. Starts Ollama with CORS enabled (if not already running)
  3. Pulls a default model if none are installed
  4. Initialises a local SQLite database for debrief history
  5. Serves the HTML frontend AND a REST API on the same port
  6. Opens the browser automatically

API endpoints (same port as the HTML server):
  GET    /api/debriefs?limit=N&offset=N  — list history, newest first
  POST   /api/debriefs                   — save a new debrief
  GET    /api/debriefs/{id}              — fetch one debrief in full
  DELETE /api/debriefs/{id}              — delete a debrief
  GET    /api/search?q=...&limit=N       — full-text search (FTS5)

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
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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

OLLAMA_API      = "http://localhost:11434"
DEFAULT_PORT    = 3000
DEFAULT_MODEL   = "llama3.2"
HTML_FILE       = "meeting_debrief.html"
DB_FILE         = "debriefs.db"
STARTUP_TIMEOUT = 30
PULL_CHUNK_SECS = 3


# ── SQLite: init & helpers ────────────────────────────────────────────────────

_DB_PATH: Optional[str] = None
_db_write_lock = threading.Lock()


def init_db(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = str(db_path)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executescript("""
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS debriefs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                transcript  TEXT    NOT NULL,
                debrief     TEXT    NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0
            );

            -- FTS5 virtual table for full-text search
            CREATE VIRTUAL TABLE IF NOT EXISTS debriefs_fts USING fts5(
                transcript,
                debrief,
                content      = debriefs,
                content_rowid = id
            );

            -- Keep FTS index in sync automatically
            CREATE TRIGGER IF NOT EXISTS debriefs_ai
            AFTER INSERT ON debriefs BEGIN
                INSERT INTO debriefs_fts(rowid, transcript, debrief)
                VALUES (new.id, new.transcript, new.debrief);
            END;

            CREATE TRIGGER IF NOT EXISTS debriefs_ad
            AFTER DELETE ON debriefs BEGIN
                INSERT INTO debriefs_fts(debriefs_fts, rowid, transcript, debrief)
                VALUES ('delete', old.id, old.transcript, old.debrief);
            END;

            CREATE TRIGGER IF NOT EXISTS debriefs_au
            AFTER UPDATE ON debriefs BEGIN
                INSERT INTO debriefs_fts(debriefs_fts, rowid, transcript, debrief)
                VALUES ('delete', old.id, old.transcript, old.debrief);
                INSERT INTO debriefs_fts(rowid, transcript, debrief)
                VALUES (new.id, new.transcript, new.debrief);
            END;
        """)


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def db_fetchall(sql: str, params: tuple = ()) -> List[Dict]:
    with _db_connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def db_fetchone(sql: str, params: tuple = ()) -> Optional[Dict]:
    with _db_connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def db_write(sql: str, params: tuple = ()) -> int:
    """Thread-safe write. Returns lastrowid."""
    with _db_write_lock:
        with _db_connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid


def extract_snippet(debrief: str) -> str:
    """Return the first non-empty line after '## Summary' as the snippet."""
    in_summary = False
    for line in debrief.split("\n"):
        stripped = line.strip()
        if "## Summary" in stripped:
            in_summary = True
            continue
        if in_summary and stripped.startswith("##"):
            break
        if in_summary and stripped:
            return stripped[:180]
    return debrief.replace("\n", " ").strip()[:180]


def row_to_summary(row: Dict) -> Dict:
    return {
        "id":          row["id"],
        "created_at":  row["created_at"],
        "model":       row["model"],
        "token_count": row["token_count"],
        "snippet":     extract_snippet(row["debrief"]),
    }


# ── HTTP handler: file serving + REST API ─────────────────────────────────────

class _Handler(http.server.SimpleHTTPRequestHandler):
    """
    Serves static files for any path that doesn't start with /api/.
    Handles the REST API for paths that do.
    """

    # ── Logging ──
    def log_message(self, fmt: str, *args) -> None:
        pass  # suppress per-request output

    # ── CORS / security headers ──
    def end_headers(self) -> None:
        self.send_header("Cross-Origin-Opener-Policy",   "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()

    # ── HTTP methods ──
    def do_OPTIONS(self) -> None:
        self._json_response(204, None)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._route_api("GET", parsed)
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._route_api("POST", parsed)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._route_api("DELETE", parsed)

    # ── Router ──
    def _route_api(self, method: str, parsed) -> None:
        path  = parsed.path
        query = parse_qs(parsed.query)

        try:
            # GET /api/debriefs
            if method == "GET" and path == "/api/debriefs":
                limit  = int(query.get("limit",  ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                rows   = db_fetchall(
                    "SELECT * FROM debriefs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
                self._json_response(200, [row_to_summary(r) for r in rows])

            # POST /api/debriefs
            elif method == "POST" and path == "/api/debriefs":
                body  = self._read_body()
                now   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                new_id = db_write(
                    "INSERT INTO debriefs (created_at, model, transcript, debrief, token_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, body["model"], body["transcript"],
                     body["debrief"], body.get("token_count", 0)),
                )
                self._json_response(201, {"id": new_id, "created_at": now})

            # GET /api/debriefs/{id}
            elif method == "GET" and re.match(r"^/api/debriefs/(\d+)$", path):
                debrief_id = int(re.match(r"^/api/debriefs/(\d+)$", path).group(1))
                row = db_fetchone("SELECT * FROM debriefs WHERE id = ?", (debrief_id,))
                if row:
                    self._json_response(200, {**row, "snippet": extract_snippet(row["debrief"])})
                else:
                    self._json_response(404, {"error": f"Debrief {debrief_id} not found"})

            # DELETE /api/debriefs/{id}
            elif method == "DELETE" and re.match(r"^/api/debriefs/(\d+)$", path):
                debrief_id = int(re.match(r"^/api/debriefs/(\d+)$", path).group(1))
                db_write("DELETE FROM debriefs WHERE id = ?", (debrief_id,))
                self._json_response(204, None)

            # GET /api/search?q=...
            elif method == "GET" and path == "/api/search":
                q     = query.get("q", [""])[0].strip()
                limit = int(query.get("limit", ["50"])[0])
                if not q:
                    self._json_response(400, {"error": "q parameter required"})
                    return
                rows = db_fetchall(
                    """
                    SELECT d.* FROM debriefs_fts
                    JOIN debriefs d ON d.id = debriefs_fts.rowid
                    WHERE debriefs_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (q, limit),
                )
                self._json_response(200, [row_to_summary(r) for r in rows])

            else:
                self._json_response(404, {"error": "Not found"})

        except KeyError as exc:
            self._json_response(400, {"error": f"Missing field: {exc}"})
        except Exception as exc:
            self._json_response(500, {"error": str(exc)})

    # ── Helpers ──
    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _json_response(self, status: int, body: Any) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body is not None:
            self.wfile.write(json.dumps(body).encode())


# ── Ollama: locate ────────────────────────────────────────────────────────────

def find_ollama() -> Optional[str]:
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
    else:
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
    global _ollama_proc, _ollama_we_started
    env = os.environ.copy()
    env["OLLAMA_ORIGINS"] = "*"
    try:
        kwargs: Dict = {}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _ollama_proc = subprocess.Popen(
            [ollama_path, "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
        )
        _ollama_we_started = True
        return True
    except Exception as exc:
        err(f"Could not start Ollama: {exc}")
        return False


def wait_for_ollama(timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if is_ollama_running():
            print()
            return True
        sys.stdout.write(f"\r  →  Waiting for Ollama{'.' * (dots % 4)}   ")
        sys.stdout.flush()
        dots += 1
        time.sleep(0.5)
    print()
    return False


def stop_ollama():
    if _ollama_we_started and _ollama_proc and _ollama_proc.poll() is None:
        _ollama_proc.terminate()
        try:
            _ollama_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _ollama_proc.kill()


# ── Ollama: models ────────────────────────────────────────────────────────────

def list_models() -> List[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def pull_model(ollama_path: str, model: str) -> bool:
    info(f"Pulling '{model}' — this only happens once, then it's cached.")
    info("Typical sizes: llama3.2 ≈ 2 GB · mistral ≈ 4 GB · phi4 ≈ 9 GB")
    blank()
    env = os.environ.copy()
    last_print = 0.0
    try:
        proc = subprocess.Popen(
            [ollama_path, "pull", model], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            now = time.time()
            if line.strip() and now - last_print > PULL_CHUNK_SECS:
                dim(f"     {line.strip()}")
                last_print = now
        proc.wait()
        return proc.returncode == 0
    except Exception as exc:
        err(f"Pull failed: {exc}")
        return False


# ── HTTP server ───────────────────────────────────────────────────────────────

def find_free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", preferred)) != 0:
            return preferred
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


_server: Optional[http.server.HTTPServer] = None


def start_http_server(port: int, directory: Path) -> int:
    global _server
    port = find_free_port(port)
    os.chdir(directory)
    _server = http.server.HTTPServer(("", port), _Handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    return port


def stop_http_server():
    if _server:
        _server.shutdown()


# ── Check mode ────────────────────────────────────────────────────────────────

def run_check(ollama_path: Optional[str]) -> bool:
    bold("  Checking environment…")
    blank()
    all_good = True

    pv = sys.version_info
    ok(f"Python {pv.major}.{pv.minor}.{pv.micro}")

    here = Path(__file__).parent.resolve()
    if (here / HTML_FILE).exists():
        ok(f"{HTML_FILE} found")
    else:
        err(f"{HTML_FILE} not found (expected alongside launch.py)")
        all_good = False

    if ollama_path:
        ok(f"Ollama binary: {ollama_path}")
    else:
        err("Ollama not found — download from https://ollama.com/download")
        all_good = False

    if is_ollama_running():
        ok("Ollama is running")
        models = list_models()
        if models:
            ok(f"Models installed: {', '.join(m.split(':')[0] for m in models)}")
        else:
            warn("No models installed yet — launcher will pull one on first run")
    else:
        warn("Ollama is not currently running (launcher will start it automatically)")

    db_path = here / DB_FILE
    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM debriefs").fetchone()[0]
            ok(f"Database found: {count} debrief(s) stored")
        except Exception:
            warn("Database file exists but could not be read — it will be recreated")
    else:
        ok("Database will be created on first run")

    blank()
    if all_good:
        ok("Everything looks good — run launch.py to start.")
    else:
        err("Some issues need fixing before the app will work.")
    return all_good


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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Meeting Debrief Assistant — launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port",       type=int, default=DEFAULT_PORT)
    parser.add_argument("--model",      type=str, default=DEFAULT_MODEL)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check",      action="store_true")
    args = parser.parse_args()

    blank()
    bold("  🗒  Meeting Debrief Assistant")
    dim( "  ─────────────────────────────────────────")
    blank()

    ollama_path = find_ollama()

    if args.check:
        run_check(ollama_path)
        sys.exit(0)

    if not ollama_path:
        err("Ollama is not installed.")
        blank()
        dim("  Download it from: https://ollama.com/download")
        sys.exit(1)
    ok(f"Ollama found: {ollama_path}")

    here = Path(__file__).parent.resolve()

    if not (here / HTML_FILE).exists():
        err(f"'{HTML_FILE}' not found.")
        dim(f"  Expected: {here / HTML_FILE}")
        dim( "  Keep launch.py and meeting_debrief.html in the same folder.")
        sys.exit(1)

    # ── Init database ──
    db_path = here / DB_FILE
    init_db(db_path)
    ok(f"Database ready: {DB_FILE}")

    # ── Start Ollama ──
    if is_ollama_running():
        ok("Ollama is already running")
    else:
        info("Starting Ollama…")
        if not start_ollama(ollama_path):
            err("Failed to launch Ollama — try: ollama serve")
            sys.exit(1)
        if not wait_for_ollama():
            err(f"Ollama didn't respond within {STARTUP_TIMEOUT}s.")
            dim("  Check port 11434 isn't blocked by a firewall.")
            sys.exit(1)
        ok("Ollama started")

    # ── Ensure a model ──
    models      = list_models()
    model_ready = any(args.model in m for m in models)

    if model_ready:
        ok(f"Model '{args.model}' is ready")
    elif models:
        first = models[0].split(":")[0]
        warn(f"'{args.model}' not installed — using '{first}' instead.")
        dim(f"  To install {args.model} later: ollama pull {args.model}")
        args.model = first
    else:
        blank()
        if not pull_model(ollama_path, args.model):
            err(f"Could not pull '{args.model}' — try: ollama pull {args.model}")
            sys.exit(1)
        ok(f"Model '{args.model}' ready")

    # ── Start server ──
    port = start_http_server(args.port, here)
    if port != args.port:
        warn(f"Port {args.port} was in use — using {port} instead.")
    url = f"http://localhost:{port}/{HTML_FILE}"
    ok(f"Serving: {url}")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        ok("Opening browser…")

    blank()
    bold("  ✦  Ready! Press Ctrl+C to stop.")
    dim( f"     App    → {url}")
    dim( f"     Model  → {args.model}")
    dim( f"     DB     → {db_path}")
    blank()

    try:
        while True:
            time.sleep(2)
            if _ollama_we_started and _ollama_proc and _ollama_proc.poll() is not None:
                warn("Ollama stopped unexpectedly — restarting…")
                if start_ollama(ollama_path) and wait_for_ollama(timeout=15):
                    ok("Ollama restarted")
                else:
                    err("Could not restart Ollama — refresh browser if app stops working.")
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()