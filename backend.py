"""
Meeting Debrief Assistant — FastAPI Backend
============================================
Handles:
  • Ollama streaming (proxied to frontend via SSE)
  • SQLite persistence with FTS5 full-text search
  • CRUD endpoints for debrief history

Requirements:
    pip install fastapi uvicorn ollama

Run:
    uvicorn backend:app --reload --port 8000

Endpoints:
    POST   /analyse              Stream debrief from Ollama, save to DB on completion
    GET    /history              List all past debriefs (paginated)
    GET    /history/{id}         Get a single debrief in full
    DELETE /history/{id}         Delete a debrief
    GET    /search?q=<query>     Full-text search across transcripts + debriefs
    GET    /stats                Aggregate stats (total debriefs, avg action items, etc.)
"""

import json
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

import ollama
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# ── Database ──────────────────────────────────────────────────────────────────

DB_PATH = "debriefs.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables on first run."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS debriefs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                transcript  TEXT    NOT NULL,
                debrief     TEXT    NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                title       TEXT    GENERATED ALWAYS AS (
                                substr(transcript, 1, 80)
                            ) VIRTUAL
            );

            -- FTS5 virtual table for full-text search
            CREATE VIRTUAL TABLE IF NOT EXISTS debriefs_fts USING fts5(
                transcript,
                debrief,
                content      = debriefs,
                content_rowid = id
            );

            -- Keep FTS index in sync automatically
            CREATE TRIGGER IF NOT EXISTS debriefs_ai AFTER INSERT ON debriefs BEGIN
                INSERT INTO debriefs_fts(rowid, transcript, debrief)
                VALUES (new.id, new.transcript, new.debrief);
            END;

            CREATE TRIGGER IF NOT EXISTS debriefs_ad AFTER DELETE ON debriefs BEGIN
                INSERT INTO debriefs_fts(debriefs_fts, rowid, transcript, debrief)
                VALUES ('delete', old.id, old.transcript, old.debrief);
            END;

            CREATE TRIGGER IF NOT EXISTS debriefs_au AFTER UPDATE ON debriefs BEGIN
                INSERT INTO debriefs_fts(debriefs_fts, rowid, transcript, debrief)
                VALUES ('delete', old.id, old.transcript, old.debrief);
                INSERT INTO debriefs_fts(rowid, transcript, debrief)
                VALUES (new.id, new.transcript, new.debrief);
            END;
        """)


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    print("✓ Database initialised:", DB_PATH)
    yield


app = FastAPI(
    title="Meeting Debrief Assistant API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Prompt ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert meeting analyst. When given a meeting transcript,
you produce a concise, well-structured debrief in Markdown. Always respond with ONLY
the following four sections — nothing else before or after:

## Summary
A single paragraph (3–5 sentences) capturing the meeting's purpose and key outcomes.

## Action Items
A Markdown table with columns: | # | Action | Owner | Due Date |
If the owner or due date is not mentioned, use "TBD".

## Decisions Made
A numbered list of concrete decisions that were agreed upon.

## Open Questions
A numbered list of questions or issues that were raised but not resolved.

Be concise. Use plain language. Do not invent information not present in the transcript."""


# ── Helper ────────────────────────────────────────────────────────────────────

def count_tokens(text: str) -> int:
    """Rough token estimate (words × 1.3)."""
    return int(len(text.split()) * 1.3)


def extract_action_item_count(debrief: str) -> int:
    """Count rows in the Action Items table (excludes header + separator rows)."""
    in_table = False
    count = 0
    for line in debrief.splitlines():
        if "## Action Items" in line:
            in_table = True
            continue
        if in_table and line.startswith("##"):
            break
        if in_table and line.startswith("|") and not re.match(r"^\|[-| ]+\|", line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Skip header row (first cell is "#" or similar non-numeric)
            if cells and cells[0].isdigit():
                count += 1
    return count


# ── Request / Response models ─────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    transcript: str
    model: str = "llama3.2"


class DebriefSummary(BaseModel):
    id: int
    created_at: str
    model: str
    token_count: int
    action_item_count: int
    snippet: str          # first 120 chars of debrief


class DebriefFull(DebriefSummary):
    transcript: str
    debrief: str


class StatsResponse(BaseModel):
    total_debriefs: int
    avg_token_count: float
    avg_action_items: float
    top_model: str
    latest_at: str | None


# ── SSE streaming ─────────────────────────────────────────────────────────────

def sse(event_type: str, **data) -> str:
    payload = json.dumps({"type": event_type, **data})
    return f"data: {payload}\n\n"


async def stream_and_save(transcript: str, model: str) -> AsyncIterator[str]:
    """Stream tokens from Ollama; persist complete debrief to SQLite."""
    full_debrief = ""

    try:
        stream = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": f"Here is the meeting transcript:\n\n{transcript}"},
            ],
            stream=True,
        )

        for chunk in stream:
            token = chunk["message"]["content"]
            full_debrief += token
            yield sse("token", content=token)

    except Exception as e:
        yield sse("error", message=str(e))
        return

    # Persist to SQLite
    token_count = count_tokens(full_debrief)
    created_at  = datetime.utcnow().isoformat()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO debriefs (created_at, model, transcript, debrief, token_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (created_at, model, transcript, full_debrief, token_count),
        )
        debrief_id = cursor.lastrowid

    yield sse("done", id=debrief_id, token_count=token_count, created_at=created_at)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/analyse")
async def analyse(req: AnalyseRequest):
    """Stream a debrief and persist it on completion."""
    if not req.transcript.strip():
        raise HTTPException(400, "Transcript cannot be empty.")
    return StreamingResponse(
        stream_and_save(req.transcript, req.model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/history", response_model=list[DebriefSummary])
def list_history(
    limit:  int = Query(20, ge=1, le=100),
    offset: int = Query(0,  ge=0),
):
    """Return paginated list of past debriefs, newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, model, debrief, token_count "
            "FROM debriefs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    return [
        DebriefSummary(
            id=r["id"],
            created_at=r["created_at"],
            model=r["model"],
            token_count=r["token_count"],
            action_item_count=extract_action_item_count(r["debrief"]),
            snippet=r["debrief"][:120].replace("\n", " "),
        )
        for r in rows
    ]


@app.get("/history/{debrief_id}", response_model=DebriefFull)
def get_debrief(debrief_id: int):
    """Retrieve a single debrief in full."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM debriefs WHERE id = ?", (debrief_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, f"Debrief {debrief_id} not found.")
    return DebriefFull(
        id=row["id"],
        created_at=row["created_at"],
        model=row["model"],
        transcript=row["transcript"],
        debrief=row["debrief"],
        token_count=row["token_count"],
        action_item_count=extract_action_item_count(row["debrief"]),
        snippet=row["debrief"][:120].replace("\n", " "),
    )


@app.delete("/history/{debrief_id}", status_code=204)
def delete_debrief(debrief_id: int):
    """Delete a debrief by ID."""
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM debriefs WHERE id = ?", (debrief_id,)
        )
    if result.rowcount == 0:
        raise HTTPException(404, f"Debrief {debrief_id} not found.")


@app.get("/search", response_model=list[DebriefSummary])
def search(
    q:      str = Query(..., min_length=2, description="Full-text search query"),
    limit:  int = Query(20, ge=1, le=100),
):
    """
    Full-text search across transcript and debrief content using SQLite FTS5.
    Supports phrase queries ("action items"), prefix queries (meet*), and
    boolean operators (AND, OR, NOT).
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.created_at, d.model, d.debrief, d.token_count
            FROM debriefs_fts
            JOIN debriefs d ON d.id = debriefs_fts.rowid
            WHERE debriefs_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()

    return [
        DebriefSummary(
            id=r["id"],
            created_at=r["created_at"],
            model=r["model"],
            token_count=r["token_count"],
            action_item_count=extract_action_item_count(r["debrief"]),
            snippet=r["debrief"][:120].replace("\n", " "),
        )
        for r in rows
    ]


@app.get("/stats", response_model=StatsResponse)
def get_stats():
    """Return aggregate statistics across all stored debriefs."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)          AS total,
                AVG(token_count)  AS avg_tokens,
                MAX(created_at)   AS latest_at,
                (SELECT model FROM debriefs
                 GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1) AS top_model
            FROM debriefs
        """).fetchone()

        # Average action items requires Python-side aggregation
        debriefs = conn.execute("SELECT debrief FROM debriefs").fetchall()

    avg_actions = (
        sum(extract_action_item_count(d["debrief"]) for d in debriefs) / len(debriefs)
        if debriefs else 0.0
    )

    return StatsResponse(
        total_debriefs=row["total"] or 0,
        avg_token_count=round(row["avg_tokens"] or 0, 1),
        avg_action_items=round(avg_actions, 1),
        top_model=row["top_model"] or "—",
        latest_at=row["latest_at"],
    )