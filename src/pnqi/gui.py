from __future__ import annotations

import multiprocessing
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .admin import ensure_startup_admin, without_elevated_flag
from .errors import OperationCancelled, PnqiError
from .formatting import human_mtime, human_size
from .indexer import (
    StagedIndex,
    browse_children,
    build_index_staged,
    commit_staged_index,
    discard_staged_index,
    iter_search,
    list_sizes,
    planned_staged_index,
    refresh_known_indexes,
)
from .progress import CancellationToken, ProgressUpdate


class _QueuedProgress:
    def __init__(self, task_queue: Any, *, min_interval: float = 0.08) -> None:
        self._task_queue = task_queue
        self._min_interval = min_interval
        self._last_sent = 0.0
        self._last_stage = ""

    def __call__(self, update: ProgressUpdate) -> None:
        now = time.monotonic()
        urgent = update.stage in {"start", "done", "ready"} or update.stage != self._last_stage
        complete = update.total is not None and update.current == update.total
        if urgent or complete or now - self._last_sent >= self._min_interval:
            self._task_queue.put(("progress", update))
            self._last_sent = now
            self._last_stage = update.stage


def _run_process_task(task: str, payload: Any, task_queue: Any, cancel_event: Any) -> None:
    token = CancellationToken(cancel_event)
    progress = _QueuedProgress(task_queue)
    try:
        if task == "refresh":
            result = refresh_known_indexes(progress=progress, token=token)
        elif task == "index":
            path, staged = payload
            result = build_index_staged(path, progress=progress, token=token, staged=staged)
        elif task == "search":
            pattern, limit = payload
            chunk: list[Any] = []
            found = 0
            for entry in iter_search(pattern, limit=limit, progress=progress, token=token):
                chunk.append(entry)
                found += 1
                if len(chunk) >= 100:
                    task_queue.put(("result-chunk", chunk))
                    chunk = []
            if chunk:
                task_queue.put(("result-chunk", chunk))
            result = {"rows": found}
        elif task == "sizes":
            result = list_sizes(payload, recursive=True, progress=progress, token=token)
        elif task == "browse":
            result = browse_children(payload, progress=progress, token=token)
        else:
            raise PnqiError(f"Unknown GUI task: {task}")
    except OperationCancelled:
        task_queue.put(("cancelled", None))
    except PnqiError as exc:
        task_queue.put(("error", ("pnqi", str(exc))))
    except BaseException as exc:
        task_queue.put(("error", ("unexpected", repr(exc))))
    else:
        task_queue.put(("done", result))


class PnqiApp(tk.Tk):
    def __init__(self, *, skip_startup_refresh: bool = False) -> None:
        super().__init__()
        self.title("py-ntfs-quick-index")
        self.geometry("980x680")
        self.minsize(780, 500)
        self._task_queue: Any = multiprocessing.Queue()
        self._task_process: multiprocessing.Process | None = None
        self._task_cancel_event: Any | None = None
        self._task_done: Callable[[Any], None] | None = None
        self._task_staged_index: StagedIndex | None = None
        self._task_cancelling = False
        self._streaming_results = False
        self._streamed_result_count = 0
        self._locked_widgets: list[tk.Widget] = []
        self._current_browse_path = tk.StringVar()
        self._status_var = tk.StringVar(value="Ready")
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_indeterminate = False
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(80, self._drain_queue)
        if not skip_startup_refresh:
            self.after(250, self._startup_refresh)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self, padding=(10, 10, 10, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(toolbar, text="Folder").grid(row=0, column=0, padx=(0, 6))
        self.folder_var = tk.StringVar()
        folder_entry = ttk.Entry(toolbar, textvariable=self.folder_var)
        folder_entry.grid(row=0, column=1, sticky="ew")
        browse_button = ttk.Button(toolbar, text="Choose", command=self._choose_folder)
        browse_button.grid(row=0, column=2, padx=(6, 0))
        index_button = ttk.Button(toolbar, text="Create Index", command=self._create_index)
        index_button.grid(row=0, column=3, padx=(6, 0))
        open_button = ttk.Button(toolbar, text="Open", command=self._open_folder)
        open_button.grid(row=0, column=4, padx=(6, 0))

        ttk.Label(toolbar, text="Pattern").grid(row=1, column=0, padx=(0, 6), pady=(8, 0))
        self.pattern_var = tk.StringVar()
        pattern_entry = ttk.Entry(toolbar, textvariable=self.pattern_var)
        pattern_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(toolbar, text="Max rows").grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        self.search_limit_var = tk.StringVar(value="1000")
        limit_spin = ttk.Spinbox(
            toolbar,
            from_=0,
            to=1_000_000,
            increment=100,
            width=8,
            textvariable=self.search_limit_var,
        )
        limit_spin.grid(row=1, column=3, padx=(6, 0), pady=(8, 0))
        search_button = ttk.Button(toolbar, text="Search", command=self._run_search)
        search_button.grid(row=1, column=4, padx=(6, 0), pady=(8, 0))
        sizes_button = ttk.Button(toolbar, text="Sizes", command=self._run_sizes)
        sizes_button.grid(row=1, column=5, padx=(6, 0), pady=(8, 0))
        self.cancel_button = ttk.Button(toolbar, text="Cancel", command=self._cancel_task, state="disabled")
        self.cancel_button.grid(row=1, column=6, padx=(6, 0), pady=(8, 0))

        self._locked_widgets = [
            folder_entry,
            browse_button,
            index_button,
            open_button,
            pattern_entry,
            limit_spin,
            search_button,
            sizes_button,
        ]

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))

        left = ttk.Frame(paned)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        paned.add(left, weight=3)

        breadcrumb = ttk.Frame(left)
        breadcrumb.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        breadcrumb.columnconfigure(1, weight=1)
        up_button = ttk.Button(breadcrumb, text="Up", command=self._browse_up)
        up_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Label(breadcrumb, textvariable=self._current_browse_path).grid(row=0, column=1, sticky="ew")
        self._locked_widgets.append(up_button)

        columns = ("size", "kind", "mtime", "path")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("kind", text="Type")
        self.tree.heading("mtime", text="Modified")
        self.tree.heading("path", text="Path")
        self.tree.column("#0", width=230, stretch=True)
        self.tree.column("size", width=100, anchor="e", stretch=False)
        self.tree.column("kind", width=80, stretch=False)
        self.tree.column("mtime", width=150, anchor="e", stretch=False)
        self.tree.column("path", width=360, stretch=True)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._tree_double_click)
        tree_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        tree_scroll.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        right = ttk.Frame(paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        paned.add(right, weight=2)
        ttk.Label(right, text="Search and size results").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.results = ttk.Treeview(right, columns=columns, show="tree headings")
        self.results.heading("#0", text="Name")
        self.results.heading("size", text="Size")
        self.results.heading("kind", text="Type")
        self.results.heading("mtime", text="Modified")
        self.results.heading("path", text="Path")
        self.results.column("#0", width=220, stretch=True)
        self.results.column("size", width=100, anchor="e", stretch=False)
        self.results.column("kind", width=80, stretch=False)
        self.results.column("mtime", width=150, anchor="e", stretch=False)
        self.results.column("path", width=360, stretch=True)
        self.results.grid(row=1, column=0, sticky="nsew")
        self.results.bind("<Double-1>", self._results_double_click)
        result_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.results.yview)
        result_scroll.grid(row=1, column=1, sticky="ns")
        self.results.configure(yscrollcommand=result_scroll.set)
        self._locked_widgets.extend([self.tree, self.results])

        status = ttk.Frame(self, padding=(10, 0, 10, 10))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self._status_var).grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(status, variable=self._progress_var, maximum=100)
        self.progress.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        status.columnconfigure(1, weight=1)

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.folder_var.set(selected)
            if not self.pattern_var.get():
                self.pattern_var.set(selected.rstrip("\\/") + "\\*")

    def _startup_refresh(self) -> None:
        self._run_task("Refreshing existing indexes", "refresh", None)

    def _create_index(self) -> None:
        path = self.folder_var.get().strip()
        if not path:
            messagebox.showwarning("Folder required", "Choose a folder first.")
            return
        try:
            staged = planned_staged_index(path)
        except PnqiError as exc:
            messagebox.showerror("pnqi", str(exc))
            return
        self._run_task("Creating index", "index", (path, staged), self._after_index, staged_index=staged)

    def _open_folder(self) -> None:
        path = self.folder_var.get().strip()
        if not path:
            messagebox.showwarning("Folder required", "Choose a folder first.")
            return
        self._browse(path)

    def _run_search(self) -> None:
        pattern = self.pattern_var.get().strip()
        if not pattern:
            messagebox.showwarning("Pattern required", "Enter a wildcard path first.")
            return
        try:
            limit = int(self.search_limit_var.get().strip() or "0")
        except ValueError:
            messagebox.showwarning("Invalid limit", "Max rows must be a non-negative integer.")
            return
        if limit < 0:
            messagebox.showwarning("Invalid limit", "Max rows must be a non-negative integer.")
            return
        self._run_task(
            "Searching",
            "search",
            (pattern, limit),
            self._after_streaming_results,
            stream_results=True,
        )

    def _run_sizes(self) -> None:
        path = self.folder_var.get().strip() or self._current_browse_path.get().strip()
        if not path:
            messagebox.showwarning("Folder required", "Choose a folder first.")
            return
        self._run_task("Loading sizes", "sizes", path, self._show_results)

    def _browse(self, path: str) -> None:
        self._run_task("Loading folder", "browse", path, self._show_browse)

    def _browse_up(self) -> None:
        current = self._current_browse_path.get().strip()
        if not current:
            return
        parent = current.rstrip("\\/")
        if len(parent) <= 2:
            parent = parent + "\\"
        else:
            parent = parent.rsplit("\\", 1)[0]
            if len(parent) == 2:
                parent += "\\"
        self._browse(parent)

    def _tree_double_click(self, _event: object) -> None:
        if self._is_task_running():
            return
        item = self.tree.focus()
        if not item:
            return
        values = self.tree.item(item, "values")
        if len(values) >= 4 and values[1] == "Folder":
            self._browse(values[3])

    def _results_double_click(self, _event: object) -> None:
        if self._is_task_running():
            return
        item = self.results.focus()
        if not item:
            return
        values = self.results.item(item, "values")
        if len(values) >= 4 and values[1] == "Folder":
            self.folder_var.set(values[3])
            self._browse(values[3])

    def _run_task(
        self,
        label: str,
        task: str,
        payload: Any,
        done: Callable[[Any], None] | None = None,
        *,
        staged_index: StagedIndex | None = None,
        stream_results: bool = False,
    ) -> None:
        if self._is_task_running():
            return
        self._clear_task_queue()
        cancel_event = multiprocessing.Event()
        self._task_cancel_event = cancel_event
        self._task_done = done
        self._task_staged_index = staged_index
        self._task_cancelling = False
        self._streaming_results = stream_results
        self._streamed_result_count = 0
        self._set_locked(True)
        self._status_var.set(label)
        self._progress_var.set(0)
        self._set_indeterminate(True)
        if stream_results:
            self.results.delete(*self.results.get_children())

        self._task_process = multiprocessing.Process(
            target=_run_process_task,
            args=(task, payload, self._task_queue, cancel_event),
            daemon=True,
        )
        self._task_process.start()

    def _cancel_task(self) -> None:
        if self._task_cancel_event is not None:
            self._task_cancel_event.set()
        self._task_cancelling = True
        if self._task_process is not None and self._task_process.is_alive():
            self._task_process.terminate()
        self.cancel_button.configure(state="disabled")
        self._status_var.set("Cancelling...")
        self.after(50, self._finish_cancelled_task)

    def _drain_queue(self) -> None:
        handled = 0
        while True:
            try:
                kind, payload = self._task_queue.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if kind == "progress":
                if not self._task_cancelling:
                    self._handle_progress(payload)
            elif kind == "result-chunk":
                if not self._task_cancelling:
                    self._append_results(payload)
            elif kind == "done":
                if self._task_cancelling:
                    self._finish_cancelled_task()
                else:
                    self._finish_task(payload)
            elif kind == "cancelled":
                self._finish_cancelled_task()
            elif kind == "error":
                if self._task_cancelling:
                    self._finish_cancelled_task()
                else:
                    self._finish_error(payload)
            if handled >= 8 and kind in {"progress", "result-chunk"}:
                break
        self.after(80, self._drain_queue)

    def _is_task_running(self) -> bool:
        return self._task_process is not None and self._task_process.is_alive()

    def _clear_task_queue(self) -> None:
        while True:
            try:
                self._task_queue.get_nowait()
            except queue.Empty:
                return

    def _join_task_process(self) -> None:
        if self._task_process is not None:
            self._task_process.join(timeout=1.0)
            if self._task_process.is_alive():
                self._task_process.terminate()
                self._task_process.join(timeout=1.0)
            if not self._task_process.is_alive():
                self._task_process.close()
        self._task_process = None
        self._task_cancel_event = None

    def _finish_task(self, result: Any) -> None:
        self._join_task_process()
        self._set_locked(False)
        self._set_indeterminate(False)
        done = self._task_done
        staged = self._task_staged_index
        self._task_done = None
        self._task_staged_index = None
        self._task_cancelling = False
        self._streaming_results = False
        try:
            if staged is not None:
                result = commit_staged_index(result if isinstance(result, StagedIndex) else staged)
            self._status_var.set("Ready")
            if done is not None:
                done(result)
        except PnqiError as exc:
            self._status_var.set("Error")
            if staged is not None:
                discard_staged_index(staged)
            messagebox.showerror("pnqi", str(exc))

    def _finish_cancelled_task(self) -> None:
        if self._task_process is not None and self._task_process.is_alive():
            self.after(50, self._finish_cancelled_task)
            return
        self._join_task_process()
        if self._task_staged_index is not None:
            discard_staged_index(self._task_staged_index)
        self._task_done = None
        self._task_staged_index = None
        self._task_cancelling = False
        self._streaming_results = False
        self._clear_task_queue()
        self._set_locked(False)
        self._set_indeterminate(False)
        self._status_var.set("Cancelled")

    def _finish_error(self, payload: Any) -> None:
        self._join_task_process()
        if self._task_staged_index is not None:
            discard_staged_index(self._task_staged_index)
        self._task_done = None
        self._task_staged_index = None
        self._task_cancelling = False
        self._streaming_results = False
        self._set_locked(False)
        self._set_indeterminate(False)
        error_kind, message = payload
        if error_kind == "pnqi":
            self._status_var.set("Error")
            messagebox.showerror("pnqi", message)
        else:
            self._status_var.set("Unexpected error")
            messagebox.showerror("pnqi", message)

    def _close(self) -> None:
        if self._task_process is not None and self._task_process.is_alive():
            if self._task_cancel_event is not None:
                self._task_cancel_event.set()
            self._task_process.terminate()
            self._task_process.join(timeout=1)
        if self._task_staged_index is not None:
            discard_staged_index(self._task_staged_index)
        self.destroy()

    def _handle_progress(self, update: ProgressUpdate) -> None:
        self._status_var.set(update.message or update.stage)
        if update.total and update.current is not None:
            self._set_indeterminate(False)
            self._progress_var.set(min(100.0, max(0.0, update.current / update.total * 100.0)))
        elif update.stage == "done":
            self._set_indeterminate(False)
            self._progress_var.set(100.0)
        else:
            self._set_indeterminate(True)

    def _set_indeterminate(self, enabled: bool) -> None:
        if enabled == self._progress_indeterminate:
            return
        self._progress_indeterminate = enabled
        if enabled:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _set_locked(self, locked: bool) -> None:
        state = "disabled" if locked else "normal"
        for widget in self._locked_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                try:
                    widget.state(["disabled"] if locked else ["!disabled"])  # type: ignore[attr-defined]
                except (AttributeError, tk.TclError):
                    pass
        self.cancel_button.configure(state="normal" if locked else "disabled")

    def _after_index(self, index_path: str) -> None:
        self._status_var.set(f"Index written: {index_path}")
        path = self.folder_var.get().strip()
        if path:
            self._browse(path)

    def _show_browse(self, payload: tuple[Any, list[Any]]) -> None:
        root, children = payload
        self._current_browse_path.set(root.path)
        self.folder_var.set(root.path)
        self._fill_tree(self.tree, children)

    def _show_results(self, entries: list[Any]) -> None:
        self._fill_tree(self.results, entries)
        self._status_var.set(f"{len(entries):,} rows")

    def _append_results(self, entries: list[Any]) -> None:
        self._insert_entries(self.results, entries)
        self._streamed_result_count += len(entries)
        self._status_var.set(f"{self._streamed_result_count:,} rows")

    def _after_streaming_results(self, payload: Any) -> None:
        count = (
            payload.get("rows", self._streamed_result_count)
            if isinstance(payload, dict)
            else self._streamed_result_count
        )
        self._status_var.set(f"{count:,} rows")

    def _fill_tree(self, widget: ttk.Treeview, entries: list[Any]) -> None:
        widget.delete(*widget.get_children())
        self._insert_entries(widget, entries)

    def _insert_entries(self, widget: ttk.Treeview, entries: list[Any]) -> None:
        for entry in entries:
            kind = "Folder" if entry.is_dir else "File"
            size = human_size(entry.tree_size if entry.is_dir else entry.size)
            name = entry.name or entry.path
            widget.insert(
                "",
                "end",
                text=name,
                values=(size, kind, human_mtime(entry.mtime_ns), entry.path),
            )


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in raw_args or "--help" in raw_args:
        print("usage: pnqi-gui [--skip-startup-refresh]")
        print()
        print("Launch the py-ntfs-quick-index graphical interface.")
        return 0
    if not ensure_startup_admin(raw_args, gui=True):
        return 0
    skip_startup_refresh = "--skip-startup-refresh" in without_elevated_flag(raw_args)
    try:
        app = PnqiApp(skip_startup_refresh=skip_startup_refresh)
        app.mainloop()
        return 0
    except PnqiError as exc:
        messagebox.showerror("pnqi", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
