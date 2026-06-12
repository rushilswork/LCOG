@pythonw -x "%~f0" & exit /b
import os, re, subprocess, sys, threading, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

C_BG      = "#f7f8fc"
C_CARD    = "#ffffff"
C_SIDEBAR = "#3f51b5"
C_ACCENT  = "#3f51b5"
C_ACCENT_H= "#303f9f"
C_TEXT    = "#212121"
C_MUTED   = "#757575"
C_BORDER  = "#e0e0e0"
C_SUCCESS = "#43a047"
C_ERROR   = "#e53935"
C_WARN    = "#fb8c00"
C_CONSOLE = "#1a1a2e"

PROVIDERS = ["groq", "gemini"]
ENV_VARS  = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}

STAGE_KEYWORDS = [
    ["Stage 1", "Static structure"],
    ["Stage 2", "Git history"],
    ["Stage 3", "Documentation"],
    ["Stage 4", "LLM narrative", "narrative generation"],
]

def find_cli():
    scripts = Path(sys.executable).parent / "Scripts"
    for name in ("onboard.exe", "onboard"):
        if (scripts / name).exists():
            return [str(scripts / name)]
    return [sys.executable, "-m", "onboard"]


class FlatButton(tk.Label):
    def __init__(self, parent, text, command, bg=C_ACCENT, fg="white",
                 hover=C_ACCENT_H, font_size=10, **kw):
        super().__init__(parent, text=text, bg=bg, fg=fg,
                         font=("Segoe UI", font_size, "bold"),
                         cursor="hand2", padx=18, pady=9,
                         relief="flat", **kw)
        self._bg, self._hover, self._cmd, self._enabled = bg, hover, command, True
        self.bind("<Enter>",    lambda e: self.config(bg=hover) if self._enabled else None)
        self.bind("<Leave>",    lambda e: self.config(bg=self._bg) if self._enabled else None)
        self.bind("<Button-1>", lambda e: command() if self._enabled else None)

    def set_text(self, t): self.config(text=t)
    def set_enabled(self, v):
        self._enabled = v
        self.config(bg=self._bg if v else C_BORDER, cursor="hand2" if v else "arrow")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Onboarding Generator")
        self.geometry("760x580")
        self.resizable(False, False)
        self.configure(bg=C_BG)
        self._proc = self._mkdocs = None
        self._build()

    def _build(self):
        # ── Sidebar ──────────────────────────────────────────────────────────
        sb = tk.Frame(self, bg=C_SIDEBAR, width=210)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        tk.Label(sb, text="⬡", bg=C_SIDEBAR, fg="white",
                 font=("Segoe UI", 28)).pack(pady=(28, 4))
        tk.Label(sb, text="Onboarding\nGenerator", bg=C_SIDEBAR, fg="white",
                 font=("Segoe UI", 13, "bold"), justify="center").pack()
        tk.Frame(sb, bg="white", height=1, width=160).pack(pady=18)
        tk.Label(sb, text="PIPELINE", bg=C_SIDEBAR, fg="#9fa8da",
                 font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=20)

        stages = [
            ("1", "Static analysis"),
            ("2", "Git history"),
            ("3", "Doc collection"),
            ("4", "AI narratives"),
        ]
        self._stage_rows = []
        for num, title in stages:
            row = tk.Frame(sb, bg=C_SIDEBAR)
            row.pack(fill="x", padx=16, pady=5)
            iv = tk.StringVar(value="○")
            tl = tk.Label(row, textvariable=iv, bg=C_SIDEBAR, fg="#90caf9",
                          font=("Segoe UI", 11), width=2)
            tl.pack(side="left")
            lf = tk.Frame(row, bg=C_SIDEBAR)
            lf.pack(side="left")
            tk.Label(lf, text=f"Stage {num}", bg=C_SIDEBAR, fg="#9fa8da",
                     font=("Segoe UI", 7)).pack(anchor="w")
            sl = tk.Label(lf, text=title, bg=C_SIDEBAR, fg="white",
                          font=("Segoe UI", 9, "bold"))
            sl.pack(anchor="w")
            self._stage_rows.append((iv, sl))

        tk.Label(sb, text="Python · JS · TS\nC/C++ · Java",
                 bg=C_SIDEBAR, fg="#9fa8da",
                 font=("Segoe UI", 8), justify="center").pack(side="bottom", pady=16)

        # ── Main panel ───────────────────────────────────────────────────────
        main = tk.Frame(self, bg=C_BG)
        main.pack(side="left", fill="both", expand=True, padx=20, pady=18)

        # Repo
        self._section(main, "Repository")
        rr = tk.Frame(self._sec, bg=C_CARD)
        rr.pack(fill="x")
        self.repo_var = tk.StringVar()
        tk.Entry(rr, textvariable=self.repo_var, font=("Segoe UI", 9),
                 relief="flat", bg="#f0f0f0", fg=C_TEXT).pack(
                 side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        tk.Button(rr, text="Browse…", command=self._browse,
                  bg=C_BG, fg=C_ACCENT, font=("Segoe UI", 9),
                  relief="flat", cursor="hand2",
                  activebackground=C_BORDER).pack(side="left")

        # Options
        self._section(main, "Options")
        opt = self._sec
        self.skip_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt, text="Skip AI  —  graph & analytics only, no API key needed",
                       variable=self.skip_var, command=self._toggle_ai,
                       bg=C_CARD, fg=C_TEXT, activebackground=C_CARD,
                       font=("Segoe UI", 9), selectcolor=C_CARD).pack(anchor="w", pady=(0, 6))

        self.ai_row = tk.Frame(opt, bg=C_CARD)
        self.ai_row.pack(fill="x")

        tk.Label(self.ai_row, text="Provider", bg=C_CARD, fg=C_MUTED,
                 font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        self.prov_var = tk.StringVar(value="groq")
        prov_cb = ttk.Combobox(self.ai_row, textvariable=self.prov_var,
                               values=PROVIDERS, width=10, state="readonly")
        prov_cb.grid(row=1, column=0, sticky="w", padx=(0, 16))
        prov_cb.bind("<<ComboboxSelected>>", self._on_prov)

        self.env_lbl = tk.Label(self.ai_row, text="GROQ_API_KEY", bg=C_CARD,
                                fg=C_MUTED, font=("Segoe UI", 8))
        self.env_lbl.grid(row=0, column=1, sticky="w")
        self.key_var = tk.StringVar(value=os.environ.get("GROQ_API_KEY", ""))
        tk.Entry(self.ai_row, textvariable=self.key_var, show="●",
                 width=32, font=("Segoe UI", 9), relief="flat",
                 bg="#f0f0f0").grid(row=1, column=1, ipady=5)

        tk.Label(self.ai_row, text="Max modules", bg=C_CARD, fg=C_MUTED,
                 font=("Segoe UI", 8)).grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.max_var = tk.IntVar(value=50)
        tk.Spinbox(self.ai_row, from_=5, to=200, increment=5,
                   textvariable=self.max_var, width=5,
                   font=("Segoe UI", 9), relief="flat",
                   bg="#f0f0f0").grid(row=1, column=2, padx=(16, 0), ipady=4, sticky="w")

        self._toggle_ai()

        # Action row
        act = tk.Frame(main, bg=C_BG)
        act.pack(fill="x", pady=(10, 6))
        self.gen_btn = FlatButton(act, "▶  Generate & Open", self._generate)
        self.gen_btn.pack(side="left")
        self.status_lbl = tk.Label(act, text="", bg=C_BG,
                                   font=("Segoe UI", 9), fg=C_MUTED)
        self.status_lbl.pack(side="left", padx=12)

        # Progress bar
        st = ttk.Style(); st.theme_use("default")
        st.configure("A.Horizontal.TProgressbar",
                      troughcolor=C_BORDER, background=C_ACCENT, thickness=4)
        self.prog = ttk.Progressbar(main, mode="indeterminate", length=510,
                                    style="A.Horizontal.TProgressbar")
        self.prog.pack(fill="x", pady=(0, 8))

        # Console
        self._section(main, "Output", expand=True)
        self.console = tk.Text(self._sec, font=("Consolas", 8),
                               bg=C_CONSOLE, fg="#d4d4d4",
                               relief="flat", state="disabled", wrap="word")
        scr = ttk.Scrollbar(self._sec, command=self.console.yview)
        self.console.configure(yscrollcommand=scr.set)
        self.console.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")

    def _section(self, parent, title, expand=False):
        f = tk.Frame(parent, bg=C_CARD, highlightthickness=1,
                     highlightbackground=C_BORDER)
        f.pack(fill="both" if expand else "x",
               expand=expand, pady=(0, 10))
        tk.Label(f, text=title, bg=C_CARD, fg=C_MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        inner = tk.Frame(f, bg=C_CARD)
        inner.pack(fill="both", expand=expand, padx=12, pady=(0, 10))
        self._sec = inner

    def _browse(self):
        p = filedialog.askdirectory(title="Select repository folder")
        if p: self.repo_var.set(p)

    def _toggle_ai(self):
        s = "disabled" if self.skip_var.get() else "normal"
        for w in self.ai_row.winfo_children():
            try: w.configure(state=s)
            except tk.TclError: pass

    def _on_prov(self, *_):
        p = self.prov_var.get()
        self.env_lbl.config(text=ENV_VARS.get(p, "API_KEY"))
        self.key_var.set(os.environ.get(ENV_VARS.get(p, ""), ""))

    def _log(self, t):
        self.console.configure(state="normal")
        self.console.insert("end", t)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _set_stage(self, i, icon, color):
        iv, sl = self._stage_rows[i]
        iv.set(icon); sl.config(fg=color)

    def _set_status(self, msg, color=C_MUTED):
        self.status_lbl.config(text=msg, fg=color)

    def _generate(self):
        repo = self.repo_var.get().strip()
        if not repo or not Path(repo).is_dir():
            self._set_status("  Select a valid repository folder", C_WARN); return
        if not self.skip_var.get() and not self.key_var.get().strip():
            self._set_status("  Enter an API key or enable Skip AI", C_WARN); return

        self.gen_btn.set_enabled(False)
        self.gen_btn.set_text("Generating…")
        self.prog.start(10)
        self._set_status("Running…", C_MUTED)
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")
        skip = self.skip_var.get()
        icons = ["○","○","○","—" if skip else "○"]
        for i,(iv,sl) in enumerate(self._stage_rows):
            iv.set(icons[i]); sl.config(fg="white")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        repo   = Path(self.repo_var.get().strip())
        skip   = self.skip_var.get()
        prov   = self.prov_var.get()
        key    = self.key_var.get().strip()
        output = repo / "onboarding-guide"

        cmd = find_cli() + ["analyze", str(repo)]
        if skip:
            cmd.append("--skip-llm")
        else:
            cmd += ["--provider", prov, "--api-key", key,
                    "--max-modules", str(self.max_var.get())]

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if not skip:
            env[ENV_VARS.get(prov, "API_KEY")] = key

        current = -1
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8",
                                    errors="replace", env=env)
            self._proc = proc
            for raw in proc.stdout:
                self.after(0, self._log, raw)
                for si, kws in enumerate(STAGE_KEYWORDS):
                    if any(k.lower() in raw.lower() for k in kws):
                        if si != current:
                            if current >= 0:
                                self.after(0, self._set_stage, current, "✓", C_SUCCESS)
                            current = si
                            self.after(0, self._set_stage, si, "◉", "#90caf9")
                        break
            proc.wait()
            if current >= 0:
                self.after(0, self._set_stage, current,
                           "✓" if proc.returncode == 0 else "✗",
                           C_SUCCESS if proc.returncode == 0 else C_ERROR)
            if proc.returncode != 0:
                self.after(0, self._finish, False); return

            for i in range(3 if skip else 4):
                self.after(0, self._set_stage, i, "✓", C_SUCCESS)
            if skip:
                self.after(0, self._set_stage, 3, "—", C_MUTED)

            self.after(0, self._log, "\nStarting mkdocs serve...\n")
            self.after(0, self._set_status, "Starting server…", C_MUTED)
            mkdocs = subprocess.Popen(
                ["mkdocs", "serve"], cwd=output,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            self._mkdocs = mkdocs
            for line in mkdocs.stdout:
                self.after(0, self._log, line)
                if "Serving on" in line:
                    self.after(600, lambda: webbrowser.open("http://127.0.0.1:8000"))
                    self.after(0, self._finish, True)
                    break
        except Exception as e:
            self.after(0, self._log, f"\nError: {e}\n")
            self.after(0, self._finish, False)

    def _finish(self, ok):
        self.prog.stop()
        self.gen_btn.set_enabled(True)
        self.gen_btn.set_text("▶  Generate & Open")
        self._set_status("  Done — browser opened" if ok else "  Failed — see output",
                         C_SUCCESS if ok else C_ERROR)

    def on_close(self):
        for p in (self._mkdocs, self._proc):
            try:
                if p: p.terminate()
            except Exception: pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
