"""
Meeting Debrief Assistant — Ollama Edition
==========================================
Runs 100% locally using Ollama. No API key. No data leaves your machine.

Paste or load a meeting transcript and get back a structured debrief:
  - Executive summary
  - Action items (owner + due date when mentioned)
  - Key decisions made
  - Open questions / unresolved items

Requirements:
    1. Install Ollama:        https://ollama.com/download
    2. Pull a model:          ollama pull llama3.2
    3. Install Python lib:    pip install ollama

Usage:
    # Analyse a transcript file
    python meeting_debrief_ollama.py --file transcript.txt

    # Paste text interactively
    python meeting_debrief_ollama.py

    # Use a different model
    python meeting_debrief_ollama.py --file transcript.txt --model mistral

    # Save the debrief to a Markdown file
    python meeting_debrief_ollama.py --file transcript.txt --output debrief.md

Recommended models (pick one):
    llama3.2     — fast, great quality         (~2 GB)
    mistral      — strong reasoning             (~4 GB)
    gemma3       — efficient, very accurate     (~3 GB)
"""

import ollama
import argparse
import sys
from datetime import datetime


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

DEFAULT_MODEL = "llama3.2"


# ── Core logic ────────────────────────────────────────────────────────────────

def check_model_available(model: str) -> None:
    """Verify the model is pulled locally; give a helpful error if not."""
    try:
        pulled = [m.model for m in ollama.list().models]
        # Normalize: ollama tags models as "llama3.2:latest", accept bare name too
        if not any(model in m for m in pulled):
            print(f"❌  Model '{model}' not found locally.")
            print(f"    Pull it first with:  ollama pull {model}")
            print(f"\n    Available models on this machine:")
            for m in pulled:
                print(f"      • {m}")
            sys.exit(1)
    except Exception as e:
        print(f"❌  Could not connect to Ollama: {e}")
        print("    Make sure Ollama is running:  ollama serve")
        sys.exit(1)


def analyse_transcript(transcript: str, model: str) -> str:
    """Stream the debrief from a local Ollama model."""
    print(f"\n⏳  Analysing with model '{model}' …\n")

    debrief_parts = []

    stream = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Here is the meeting transcript:\n\n{transcript}"},
        ],
        stream=True,
    )

    for chunk in stream:
        text = chunk["message"]["content"]
        print(text, end="", flush=True)
        debrief_parts.append(text)

    print("\n")
    return "".join(debrief_parts)


# ── I/O helpers ───────────────────────────────────────────────────────────────

def read_transcript_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"❌  File not found: {path}")
        sys.exit(1)
    except IOError as e:
        print(f"❌  Could not read file: {e}")
        sys.exit(1)


def read_transcript_interactively() -> str:
    print("📋  Paste your meeting transcript below.")
    print("    When done, press Enter then Ctrl+D (Mac/Linux) or Ctrl+Z + Enter (Windows).\n")
    try:
        return sys.stdin.read()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)


def save_debrief(debrief: str, path: str) -> None:
    header = (
        f"# Meeting Debrief\n"
        f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + debrief)
        print(f"💾  Debrief saved to: {path}")
    except IOError as e:
        print(f"❌  Could not save file: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Meeting Debrief Assistant — powered by Ollama (local AI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--file", "-f",
        metavar="PATH",
        help="Path to a plain-text transcript file",
    )
    parser.add_argument(
        "--model", "-m",
        metavar="MODEL",
        default=DEFAULT_MODEL,
        help=f"Ollama model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="PATH",
        help="Optional path to save the debrief as a Markdown file",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    print("=" * 55)
    print("   🗒️   Meeting Debrief Assistant (Ollama — local)")
    print("=" * 55)

    # Verify Ollama is running and model is available
    check_model_available(args.model)

    # Load transcript
    if args.file:
        print(f"\n📂  Loading transcript: {args.file}")
        transcript = read_transcript_from_file(args.file)
    else:
        transcript = read_transcript_interactively()

    if not transcript.strip():
        print("❌  Transcript is empty. Nothing to analyse.")
        sys.exit(1)

    # Analyse
    debrief = analyse_transcript(transcript, args.model)

    # Optionally save
    if args.output:
        save_debrief(debrief, args.output)


if __name__ == "__main__":
    main()