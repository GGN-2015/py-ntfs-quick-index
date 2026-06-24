from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .admin import ensure_startup_admin, without_elevated_flag
from .errors import OperationCancelled, PnqiError
from .formatting import human_mtime, human_size
from .indexer import browse_children, build_index, list_sizes, refresh_known_indexes, search
from .progress import CancellationToken, ProgressUpdate


class PnqiApp(tk.Tk):
    def __init__(self, *, skip_startup_refresh: bool = False) -> None:
        super().__init__()
        self.title("py-ntfs-quick-index")
        self.geometry("980x680")
        self.minsize(780, 500)
        self._task_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._task_thread: threading.Thread | None = None
        self._task_token: CancellationToken | None = None
        self._locked_widgets: list[tk.Widget] = []
        self._current_browse_path = tk.StringVar()
        self._status_var = tk.StringVar(value="Ready")
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_indeterminate = False
        self._build_ui()
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
        search_button = ttk.Button(toolbar, text="Search", command=self._run_search)
        search_button.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        sizes_button = ttk.Button(toolbar, text="Sizes", command=self._run_sizes)
        sizes_button.grid(row=1, column=3, padx=(6, 0), pady=(8, 0))
        self.cancel_button = ttk.Button(toolbar, text="Cancel", command=self._cancel_task, state="disabled")
        self.cancel_button.grid(row=1, column=4, padx=(6, 0), pady=(8, 0))

        self._locked_widgets = [
            folder_entry,
            browse_button,
            index_button,
            open_button,
            pattern_entry,
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
        ttk.Button(breadcrumb, text="Up", command=self._browse_up).grid(row=0, column=0, padx=(0, 6))
        ttk.Label(breadcrumb, textvariable=self._current_browse_path).grid(row=0, column=1, sticky="ew")

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
        self._run_task("Refreshing existing indexes", lambda token, progress: refresh_known_indexes(progress=progress, token=token))

    def _create_index(self) -> None:
        path = self.folder_var.get().strip()
        if not path:
            messagebox.showwarning("Folder required", "Choose a folder first.")
            return
        self._run_task("Creating index", lambda token, progress: build_index(path, progress=progress, token=token), self._after_index)

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
        self._run_task("Searching", lambda token, progress: search(pattern, progress=progress, token=token), self._show_results)

    def _run_sizes(self) -> None:
        path = self.folder_var.get().strip() or self._current_browse_path.get().strip()
        if not path:
            messagebox.showwarning("Folder required", "Choose a folder first.")
            return
        self._run_task(
            "Loading sizes",
            lambda token, progress: list_sizes(path, recursive=True, progress=progress, token=token),
            self._show_results,
        )

    def _browse(self, path: str) -> None:
        self._run_task(
            "Loading folder",
            lambda token, progress: browse_children(path, progress=progress, token=token),
            self._show_browse,
        )

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
        if self._task_thread is not None and self._task_thread.is_alive():
            return
        item = self.tree.focus()
        if not item:
            return
        values = self.tree.item(item, "values")
        if len(values) >= 4 and values[1] == "Folder":
            self._browse(values[3])

    def _results_double_click(self, _event: object) -> None:
        if self._task_thread is not None and self._task_thread.is_alive():
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
        func: Callable[[CancellationToken, Callable[[ProgressUpdate], None]], Any],
        done: Callable[[Any], None] | None = None,
    ) -> None:
        if self._task_thread is not None and self._task_thread.is_alive():
            return
        token = CancellationToken()
        self._task_token = token
        self._set_locked(True)
        self._status_var.set(label)
        self._progress_var.set(0)
        self._set_indeterminate(True)

        def progress(update: ProgressUpdate) -> None:
            self._task_queue.put(("progress", update))

        def worker() -> None:
            try:
                result = func(token, progress)
            except BaseException as exc:
                self._task_queue.put(("error", exc))
            else:
                self._task_queue.put(("done", (result, done)))

        self._task_thread = threading.Thread(target=worker, daemon=True)
        self._task_thread.start()

    def _cancel_task(self) -> None:
        if self._task_token is not None:
            self._task_token.cancel()
            self._status_var.set("Cancelling...")

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._task_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self._handle_progress(payload)
            elif kind == "done":
                result, callback = payload
                self._set_locked(False)
                self._set_indeterminate(False)
                self._status_var.set("Ready")
                if callback is not None:
                    callback(result)
            elif kind == "error":
                self._set_locked(False)
                self._set_indeterminate(False)
                exc = payload
                if isinstance(exc, OperationCancelled):
                    self._status_var.set("Cancelled")
                elif isinstance(exc, PnqiError):
                    self._status_var.set("Error")
                    messagebox.showerror("pnqi", str(exc))
                else:
                    self._status_var.set("Unexpected error")
                    messagebox.showerror("pnqi", repr(exc))
        self.after(80, self._drain_queue)

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

    def _fill_tree(self, widget: ttk.Treeview, entries: list[Any]) -> None:
        widget.delete(*widget.get_children())
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
