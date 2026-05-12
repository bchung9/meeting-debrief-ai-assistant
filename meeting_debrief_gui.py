"""
Meeting Debrief Assistant — GUI Edition
========================================
A polished desktop app for analysing meeting transcripts with local AI.

Requirements:
    pip install ollama
    ollama pull llama3.2   (or mistral / gemma3)

Run:
    python meeting_debrief_gui.py
"""

import ollama
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
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

MODELS = ["llama3.2", "mistral", "gemma3", "llama3.1", "phi4"]

# ── Theme ─────────────────────────────────────────────────────────────────────

BG        = "#0f1117"
PANEL     = "#1a1d27"
BORDER    = "#2a2d3e"
ACCENT    = "#7c6af7"
ACCENT2   = "#a78bfa"
TEXT      = "#e2e8f0"
MUTED     = "#64748b"
SUCCESS   = "#34d399"
WARNING   = "#fbbf24"
FONT_MONO = ("Courier New", 11)
FONT_UI   = ("Georgia", 11)
FONT_H1   = ("Georgia", 18, "bold")
FONT_H2   = ("Georgia", 13, "bold")
FONT_SM   = ("Georgia", 9)


# ── App ───────────────────────────────────────────────────────────────────────

class DebriefApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Meeting Debrief Assistant")
        self.geometry("1200x780")
        self.minsize(900, 600)
        self.configure(bg=BG)

        self._streaming = False
        self._setup_styles()
        self._build_ui()

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=FONT_UI)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SM)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=FONT_UI)
        style.configure("Header.TLabel", background=BG, foreground=TEXT, font=FONT_H1)
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=FONT_UI)

        style.configure(
            "Accent.TButton",
            background=ACCENT, foreground="#ffffff",
            font=("Georgia", 11, "bold"),
            borderwidth=0, relief="flat", padding=(20, 10),
        )
        style.map("Accent.TButton",
            background=[("active", ACCENT2), ("disabled", BORDER)],
            foreground=[("disabled", MUTED)],
        )

        style.configure(
            "Ghost.TButton",
            background=PANEL, foreground=TEXT,
            font=FONT_UI,
            borderwidth=1, relief="flat", padding=(14, 8),
        )
        style.map("Ghost.TButton",
            background=[("active", BORDER)],
        )

        style.configure(
            "TCombobox",
            fieldbackground=PANEL, background=PANEL,
            foreground=TEXT, selectbackground=ACCENT,
            bordercolor=BORDER, arrowcolor=TEXT,
            font=FONT_UI,
        )
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)])

        style.configure("Horizontal.TProgressbar",
            troughcolor=BORDER, background=ACCENT, borderwidth=0, thickness=3)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──
        topbar = ttk.Frame(self, style="TFrame")
        topbar.pack(fill="x", padx=28, pady=(24, 0))

        ttk.Label(topbar, text="🗒  Meeting Debrief", style="Header.TLabel").pack(side="left")
        ttk.Label(topbar, text="Powered by Ollama — runs 100% locally",
                  style="Sub.TLabel").pack(side="left", padx=(14, 0), pady=(6, 0))

        # Model picker (right-aligned)
        model_frame = ttk.Frame(topbar, style="TFrame")
        model_frame.pack(side="right")
        ttk.Label(model_frame, text="Model", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.model_var = tk.StringVar(value=MODELS[0])
        model_cb = ttk.Combobox(model_frame, textvariable=self.model_var,
                                values=MODELS, state="readonly", width=14,
                                style="TCombobox")
        model_cb.pack(side="left")

        # ── Thin accent rule ──
        rule = tk.Frame(self, height=1, bg=ACCENT)
        rule.pack(fill="x", padx=28, pady=(16, 0))

        # ── Main two-column body ──
        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True, padx=28, pady=20)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # Left heading
        left_head = ttk.Frame(body, style="TFrame")
        left_head.grid(row=0, column=0, sticky="ew", padx=(0, 10), pady=(0, 8))
        ttk.Label(left_head, text="Transcript", style="Header.TLabel",
                  font=FONT_H2).pack(side="left")

        load_btn = ttk.Button(left_head, text="📂  Load file",
                              style="Ghost.TButton", command=self._load_file)
        load_btn.pack(side="right")
        clear_btn = ttk.Button(left_head, text="✕  Clear",
                               style="Ghost.TButton", command=self._clear_transcript)
        clear_btn.pack(side="right", padx=(0, 6))

        # Right heading
        right_head = ttk.Frame(body, style="TFrame")
        right_head.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(0, 8))
        ttk.Label(right_head, text="Debrief", style="Header.TLabel",
                  font=FONT_H2).pack(side="left")

        self.save_btn = ttk.Button(right_head, text="💾  Save",
                                   style="Ghost.TButton", command=self._save_debrief,
                                   state="disabled")
        self.save_btn.pack(side="right")

        # Left panel — transcript input
        left_panel = ttk.Frame(body, style="Panel.TFrame")
        left_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 10))

        self.transcript_text = tk.Text(
            left_panel,
            bg=PANEL, fg=TEXT, insertbackground=ACCENT2,
            font=FONT_MONO, relief="flat", bd=0,
            wrap="word", padx=14, pady=14,
            selectbackground=ACCENT, selectforeground="#fff",
        )
        self.transcript_text.pack(fill="both", expand=True, side="left")
        self.transcript_text.insert("1.0", "Paste your meeting transcript here…")
        self.transcript_text.bind("<FocusIn>", self._clear_placeholder)

        t_scroll = ttk.Scrollbar(left_panel, command=self.transcript_text.yview)
        t_scroll.pack(side="right", fill="y")
        self.transcript_text.configure(yscrollcommand=t_scroll.set)

        # Right panel — debrief output
        right_panel = ttk.Frame(body, style="Panel.TFrame")
        right_panel.grid(row=1, column=1, sticky="nsew", padx=(10, 0))

        self.debrief_text = tk.Text(
            right_panel,
            bg=PANEL, fg=TEXT, insertbackground=ACCENT2,
            font=FONT_MONO, relief="flat", bd=0,
            wrap="word", padx=14, pady=14,
            state="disabled",
            selectbackground=ACCENT, selectforeground="#fff",
        )
        self.debrief_text.pack(fill="both", expand=True, side="left")

        # Tag styles for Markdown-like rendering
        self.debrief_text.tag_configure("heading",
            foreground=ACCENT2, font=("Georgia", 13, "bold"))
        self.debrief_text.tag_configure("bold",
            foreground=TEXT, font=("Courier New", 11, "bold"))
        self.debrief_text.tag_configure("muted",
            foreground=MUTED)
        self.debrief_text.tag_configure("pipe",
            foreground=ACCENT)

        d_scroll = ttk.Scrollbar(right_panel, command=self.debrief_text.yview)
        d_scroll.pack(side="right", fill="y")
        self.debrief_text.configure(yscrollcommand=d_scroll.set)

        # ── Bottom bar ──
        bottom = ttk.Frame(self, style="TFrame")
        bottom.pack(fill="x", padx=28, pady=(0, 20))

        self.progress = ttk.Progressbar(bottom, mode="indeterminate",
                                        style="Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(0, 10))

        btn_row = ttk.Frame(bottom, style="TFrame")
        btn_row.pack(fill="x")

        self.analyse_btn = ttk.Button(
            btn_row, text="✦  Analyse Transcript",
            style="Accent.TButton", command=self._start_analysis,
        )
        self.analyse_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Ready — paste a transcript or load a file.")
        ttk.Label(btn_row, textvariable=self.status_var,
                  style="Muted.TLabel").pack(side="left", padx=18)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _clear_placeholder(self, _event=None):
        if self.transcript_text.get("1.0", "end-1c") == "Paste your meeting transcript here…":
            self.transcript_text.delete("1.0", "end")

    def _clear_transcript(self):
        self.transcript_text.delete("1.0", "end")
        self.status_var.set("Transcript cleared.")

    def _load_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt *.md"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.transcript_text.delete("1.0", "end")
            self.transcript_text.insert("1.0", content)
            self.status_var.set(f"Loaded: {path.split('/')[-1]}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not read file:\n{e}")

    def _save_debrief(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt")],
            initialfile=f"debrief_{datetime.now().strftime('%Y%m%d_%H%M')}.md",
        )
        if not path:
            return
        content = self.debrief_text.get("1.0", "end-1c")
        header = f"# Meeting Debrief\n_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + content)
            self.status_var.set(f"Saved to {path.split('/')[-1]}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save file:\n{e}")

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _start_analysis(self):
        transcript = self.transcript_text.get("1.0", "end-1c").strip()
        if not transcript or transcript == "Paste your meeting transcript here…":
            messagebox.showwarning("No transcript", "Please paste or load a transcript first.")
            return

        model = self.model_var.get()
        self._set_busy(True)
        self._clear_debrief()

        thread = threading.Thread(
            target=self._run_analysis,
            args=(transcript, model),
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, transcript: str, model: str):
        try:
            # Verify Ollama is reachable
            pulled = [m.model for m in ollama.list().models]
            if not any(model in m for m in pulled):
                self.after(0, lambda: self._handle_error(
                    f"Model '{model}' not found locally.\n\nRun:  ollama pull {model}"
                ))
                return

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
                self.after(0, lambda t=token: self._append_token(t))

            self.after(0, self._done)

        except Exception as e:
            self.after(0, lambda: self._handle_error(str(e)))

    def _append_token(self, token: str):
        self.debrief_text.configure(state="normal")
        # Apply simple Markdown-style colouring on the fly
        if token.startswith("##"):
            self.debrief_text.insert("end", token, "heading")
        elif "|" in token:
            self.debrief_text.insert("end", token, "pipe")
        else:
            self.debrief_text.insert("end", token)
        self.debrief_text.configure(state="disabled")
        self.debrief_text.see("end")

    def _clear_debrief(self):
        self.debrief_text.configure(state="normal")
        self.debrief_text.delete("1.0", "end")
        self.debrief_text.configure(state="disabled")

    def _set_busy(self, busy: bool):
        self._streaming = busy
        if busy:
            self.analyse_btn.configure(state="disabled", text="⏳  Analysing…")
            self.save_btn.configure(state="disabled")
            self.progress.start(12)
            self.status_var.set(f"Streaming from {self.model_var.get()}…")
        else:
            self.analyse_btn.configure(state="normal", text="✦  Analyse Transcript")
            self.progress.stop()
            self.progress["value"] = 0

    def _done(self):
        self._set_busy(False)
        self.save_btn.configure(state="normal")
        self.status_var.set(
            f"✓  Done — {datetime.now().strftime('%H:%M:%S')}  |  "
            f"Click 💾 Save to export as Markdown."
        )

    def _handle_error(self, message: str):
        self._set_busy(False)
        self.status_var.set("❌  Error — see dialog.")
        messagebox.showerror("Ollama Error", message)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = DebriefApp()
    app.mainloop()