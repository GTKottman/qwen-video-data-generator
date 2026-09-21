#!/usr/bin/env python3
"""Minimal desktop GUI for preconvert.py.

Pick a video, optionally tweak the size/duration targets, and convert it
locally before uploading to the Qwen Video Classifier. Uses only the Python
standard library (tkinter) plus preconvert.py in this same directory.
"""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preconvert  # noqa: E402


class ConversionUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Qwen Video Classifier — Pre-Converter")
        root.geometry("640x420")
        root.minsize(560, 380)

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.max_bytes_var = tk.StringVar(value=str(preconvert.DEFAULT_TARGET_MAX_BYTES))
        self.max_duration_var = tk.StringVar(value=str(preconvert.DEFAULT_MAX_DURATION_SECONDS))
        self.dry_run_var = tk.BooleanVar(value=False)

        self._build_widgets()
        self.root.after(100, self._poll_queue)

    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 6}
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)

        row = ttk.Frame(frame)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Input video:", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self.input_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse…", command=self._browse_input).pack(side="left")

        row = ttk.Frame(frame)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Output file:", width=14).pack(side="left")
        ttk.Entry(row, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse…", command=self._browse_output).pack(side="left")

        row = ttk.Frame(frame)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Max size (bytes):", width=16).pack(side="left")
        ttk.Entry(row, textvariable=self.max_bytes_var, width=14).pack(side="left", padx=(0, 16))
        ttk.Label(row, text="Max duration (s):").pack(side="left")
        ttk.Entry(row, textvariable=self.max_duration_var, width=10).pack(side="left", padx=(6, 0))

        row = ttk.Frame(frame)
        row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="Dry run (show plan only, don't encode)", variable=self.dry_run_var).pack(
            side="left"
        )

        row = ttk.Frame(frame)
        row.pack(fill="x", **pad)
        self.start_button = ttk.Button(row, text="Start", command=self._on_start)
        self.start_button.pack(side="left")
        self.progress = ttk.Progressbar(row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[
                ("Video files", "*.mp4 *.avi *.mkv *.mov *.flv *.wmv *.webm *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.input_var.set(path)
        if not self.output_var.get():
            self.output_var.set(str(Path(path).with_suffix("").with_suffix(".preconverted.mp4")))

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save converted video as",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if path:
            self.output_var.set(path)

    def _log_line(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")

    def _on_start(self) -> None:
        input_path = Path(self.input_var.get().strip())
        if not self.input_var.get().strip():
            messagebox.showerror("Missing input", "Choose an input video first.")
            return
        if not input_path.exists():
            messagebox.showerror("Not found", f"Input file does not exist:\n{input_path}")
            return

        try:
            max_bytes = int(self.max_bytes_var.get())
            max_duration = int(self.max_duration_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Max size and max duration must be whole numbers.")
            return

        dry_run = self.dry_run_var.get()
        output_str = self.output_var.get().strip()
        output_path = Path(output_str) if output_str else input_path.with_suffix("").with_suffix(
            ".preconverted.mp4"
        )
        if not dry_run and output_path.resolve() == input_path.resolve():
            messagebox.showerror("Invalid output", "Output path must differ from the input path.")
            return

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0
        self._set_running(True)

        self._worker = threading.Thread(
            target=self._run_job, args=(input_path, output_path, max_bytes, max_duration, dry_run), daemon=True,
        )
        self._worker.start()

    def _run_job(
        self, input_path: Path, output_path: Path, max_bytes: int, max_duration: int, dry_run: bool,
    ) -> None:
        try:
            preconvert.check_dependencies()
            src = preconvert.probe(input_path)
            plan = preconvert.plan_conversion(src, max_duration, max_bytes)
            self._queue.put(("log", preconvert.describe_plan(src, plan)))

            if dry_run:
                self._queue.put(("done", None))
                return

            self._queue.put(("log", f"\nconverting -> {output_path}"))

            def on_progress(frac: float) -> None:
                self._queue.put(("progress", frac))

            preconvert.run_conversion(input_path, output_path, plan, on_progress=on_progress)
            out_size = output_path.stat().st_size
            self._queue.put(("log", f"done: {preconvert.human_bytes(out_size)} written to {output_path}"))
            self._queue.put(("done", None))
        except preconvert.PreconvertError as exc:
            self._queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected to the user
            self._queue.put(("error", f"unexpected error: {exc}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log_line(str(payload))
                elif kind == "progress":
                    self.progress["value"] = float(payload) * 100
                elif kind == "done":
                    self.progress["value"] = 100
                    self._set_running(False)
                elif kind == "error":
                    self._set_running(False)
                    self._log_line(f"error: {payload}")
                    messagebox.showerror("Conversion failed", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


def main() -> int:
    try:
        preconvert.check_dependencies()
    except preconvert.PreconvertError as exc:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Missing dependency", str(exc))
        except tk.TclError:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    root = tk.Tk()
    ConversionUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
