from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, ttk

from .archive import parse_date
from .version import __version__
from .service import ArchiveService
from .state import UiConfig, load_ui_config, save_ui_config
from .worker import ArchiveWorker, WorkerEvent


class ArchiveWindow:
    """One-window, read-only archive frontend. All Tk calls stay on the UI thread."""

    def __init__(self, root: tk.Tk, service: ArchiveService | None = None, *, autoload: bool = True) -> None:
        self.root = root
        self.service = service or ArchiveService()
        self.worker = ArchiveWorker()
        self._task = ""
        self._busy = False
        self._ready = False
        self._closing = False
        self._chats: dict[str, dict] = {}
        self._accounts: list[str] = []
        self._last_directory: Path | None = None
        self._poll_id: str | None = None
        settings = load_ui_config()
        default_root = Path.home() / "Downloads" / "wechat-local-archive"
        self.output = tk.StringVar(value=settings.output_root if settings else str(default_root))
        self.preset = tk.StringVar(value=settings.asr_preset if settings else "balanced")
        self.search = tk.StringVar()
        self.account_choice = tk.StringVar()
        self.start_date = tk.StringVar()
        self.end_date = tk.StringVar()
        self.images = tk.BooleanVar(value=True)
        self.voice = tk.BooleanVar(value=True)
        self.other_media = tk.BooleanVar(value=True)
        self.transcribe = tk.BooleanVar(value=True)
        self.transcribe.trace_add("write", lambda *_: self._update_controls() if hasattr(self, "controls") else None)
        self.status_text = tk.StringVar(value="正在检查本地状态…")
        self.account_text = tk.StringVar(value="")
        self.progress_text = tk.StringVar(value="")
        self.summary_text = tk.StringVar(value="")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = self.root.after(80, self._poll)
        if autoload:
            self._start("load", self._load)
        else:
            self._show_ready(False)

    def _build(self) -> None:
        root = self.root
        root.title("WeChat Local Archive")
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(820, max(480, screen_width - 80))}x{min(700, max(460, screen_height - 80))}")
        root.minsize(480, 460)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        fixed = font.nametofont("TkFixedFont", root=root).copy()
        fixed.configure(family="Consolas", size=10)
        self.fixed = fixed
        style.configure("Archive.TLabel", font=fixed)
        style.configure("Archive.TButton", font=fixed)
        style.configure("Archive.Treeview", font=fixed, rowheight=23)
        style.configure("Archive.Treeview.Heading", font=fixed)
        style.configure("Archive.TEntry", font=fixed)
        style.configure("Archive.TCombobox", font=fixed)
        style.configure("Archive.TCheckbutton", font=fixed)
        style.configure("Archive.Horizontal.TProgressbar", thickness=5)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer = ttk.Frame(root, padding=16)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        title = ttk.Label(outer, text="WECHAT LOCAL ARCHIVE", font=("Consolas", 13, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 10))
        info = ttk.Frame(outer)
        info.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        info.columnconfigure(1, weight=1)
        self._label(info, "STATUS", 0)
        self._label(info, "ACCOUNT", 1)
        self.status_label = ttk.Label(info, textvariable=self.status_text, style="Archive.TLabel", wraplength=610)
        self.status_label.grid(row=0, column=1, sticky="w")
        ttk.Label(info, textvariable=self.account_text, style="Archive.TLabel").grid(row=1, column=1, sticky="w")

        self.setup_frame = ttk.Frame(outer)
        self.setup_frame.grid(row=2, column=0, sticky="ew", pady=(4, 12))
        ttk.Label(self.setup_frame, text="首次使用请先登录 Windows 微信，再读取本地数据库密钥。", wraplength=650).pack(anchor="w", pady=(0, 8))
        self.account_box = ttk.Combobox(self.setup_frame, textvariable=self.account_choice, state="readonly", style="Archive.TCombobox")
        self.account_box.pack(fill="x", pady=(0, 8))
        self.initialize_button = ttk.Button(self.setup_frame, text="[ INITIALIZE ]", command=self.initialize, style="Archive.TButton")
        self.initialize_button.pack(anchor="w")

        self.ready_host = ttk.Frame(outer)
        self.ready_host.grid(row=3, column=0, sticky="nsew")
        self.ready_host.columnconfigure(0, weight=1)
        self.ready_host.rowconfigure(0, weight=1)
        self.form_canvas = tk.Canvas(self.ready_host, borderwidth=0, highlightthickness=0, background=style.lookup("TFrame", "background") or "SystemButtonFace")
        self.form_canvas.grid(row=0, column=0, sticky="nsew")
        self.form_scroll = ttk.Scrollbar(self.ready_host, orient="vertical", command=self.form_canvas.yview)
        self.form_scroll.grid(row=0, column=1, sticky="ns")
        self.form_canvas.configure(yscrollcommand=self.form_scroll.set)
        self.ready_frame = ttk.Frame(self.form_canvas)
        self._form_window = self.form_canvas.create_window((0, 0), window=self.ready_frame, anchor="nw")
        self.ready_frame.bind("<Configure>", lambda _: self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all")))
        self.form_canvas.bind("<Configure>", lambda event: self.form_canvas.itemconfigure(self._form_window, width=event.width))
        root.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.ready_frame.columnconfigure(1, weight=1)
        self._label(self.ready_frame, "CHAT", 0)
        self.search_entry = ttk.Entry(self.ready_frame, textvariable=self.search, style="Archive.TEntry")
        self.search_entry.grid(row=0, column=1, sticky="ew", pady=(0, 5))
        self.search.trace_add("write", lambda *_: self._filter_chats())
        self.chat_tree = ttk.Treeview(self.ready_frame, columns=("name", "count"), show="headings", selectmode="browse", height=5, style="Archive.Treeview")
        self.chat_tree.heading("name", text="会话")
        self.chat_tree.heading("count", text="消息")
        self.chat_tree.column("name", minwidth=120, width=300, stretch=True)
        self.chat_tree.column("count", minwidth=65, width=90, stretch=False, anchor="e")
        self.chat_tree.grid(row=1, column=1, sticky="nsew", pady=(0, 8))
        self.chat_tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self.chat_tree.bind("<Double-1>", lambda _: self.start_export())
        self._label(self.ready_frame, "RANGE", 2)
        dates = ttk.Frame(self.ready_frame)
        dates.grid(row=2, column=1, sticky="ew", pady=(0, 7))
        self.start_entry = ttk.Entry(dates, textvariable=self.start_date, width=14, style="Archive.TEntry")
        self.start_entry.pack(side="left")
        ttk.Label(dates, text="  —  ").pack(side="left")
        self.end_entry = ttk.Entry(dates, textvariable=self.end_date, width=14, style="Archive.TEntry")
        self.end_entry.pack(side="left")
        ttk.Label(dates, text="  留空=不限").pack(side="left")
        self._label(self.ready_frame, "MEDIA", 3)
        media_row = ttk.Frame(self.ready_frame)
        media_row.grid(row=3, column=1, sticky="w", pady=(0, 7))
        self.image_check = ttk.Checkbutton(media_row, text="图片", variable=self.images, style="Archive.TCheckbutton")
        self.voice_check = ttk.Checkbutton(media_row, text="语音", variable=self.voice, command=self._voice_changed, style="Archive.TCheckbutton")
        self.other_check = ttk.Checkbutton(media_row, text="视频 / 文件", variable=self.other_media, style="Archive.TCheckbutton")
        for widget in (self.image_check, self.voice_check, self.other_check):
            widget.pack(side="left", padx=(0, 12))
        self._label(self.ready_frame, "ASR", 4)
        asr_row = ttk.Frame(self.ready_frame)
        asr_row.grid(row=4, column=1, sticky="w", pady=(0, 7))
        self.asr_check = ttk.Checkbutton(asr_row, text="转写语音", variable=self.transcribe, style="Archive.TCheckbutton")
        self.asr_check.pack(side="left", padx=(0, 12))
        self.preset_box = ttk.Combobox(asr_row, textvariable=self.preset, values=("background", "balanced", "fast"), state="readonly", width=13, style="Archive.TCombobox")
        self.preset_box.pack(side="left")
        self.preset_box.bind("<<ComboboxSelected>>", lambda _: self._save_settings())
        self._label(self.ready_frame, "OUTPUT", 5)
        output_row = ttk.Frame(self.ready_frame)
        output_row.grid(row=5, column=1, sticky="ew", pady=(0, 10))
        output_row.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(output_row, textvariable=self.output, style="Archive.TEntry")
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.browse_button = ttk.Button(output_row, text="...", width=3, command=self.browse, style="Archive.TButton")
        self.browse_button.grid(row=0, column=1)
        self.output_entry.bind("<FocusOut>", lambda _: self._save_settings())
        actions = ttk.Frame(self.ready_frame)
        actions.grid(row=6, column=1, sticky="w", pady=(0, 10))
        self.export_button = ttk.Button(actions, text="[ EXPORT ]", command=self.start_export, style="Archive.TButton")
        self.export_button.pack(side="left", padx=(0, 10))
        self.verify_button = ttk.Button(actions, text="VERIFY", command=self.start_verify, style="Archive.TButton")
        self.verify_button.pack(side="left", padx=(0, 10))
        self.cancel_button = ttk.Button(actions, text="CANCEL", command=self.cancel, style="Archive.TButton")
        self.cancel_button.pack(side="left")
        self.controls = (self.search_entry, self.start_entry, self.end_entry, self.image_check, self.voice_check, self.other_check, self.asr_check, self.preset_box, self.output_entry, self.browse_button, self.export_button, self.verify_button)

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Separator(footer).grid(row=0, column=0, sticky="ew", pady=(0, 9))
        self.progress_label = ttk.Label(footer, textvariable=self.progress_text, style="Archive.TLabel", wraplength=650)
        self.progress_label.grid(row=1, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(footer, mode="determinate", maximum=100, style="Archive.Horizontal.TProgressbar")
        self.progress_bar.grid(row=2, column=0, sticky="ew", pady=(5, 7))
        self.summary_label = ttk.Label(footer, textvariable=self.summary_text, wraplength=650, justify="left")
        self.summary_label.grid(row=3, column=0, sticky="w")
        self.open_button = ttk.Button(footer, text="[ OPEN FOLDER ]", command=self.open_folder, style="Archive.TButton")
        self.open_button.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(footer, text=f"v{__version__}", style="Archive.TLabel").grid(row=4, column=1, sticky="e", pady=(8, 0))
        outer.bind("<Configure>", self._resize_labels)
        self._update_controls()

    def _on_mousewheel(self, event):
        if not self._ready or event.widget.winfo_class() == "Treeview":
            return None
        if self.form_canvas.winfo_height() < self.ready_frame.winfo_reqheight():
            self.form_canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"
        return None

    def _resize_labels(self, event) -> None:
        width = max(180, event.width - 120)
        for label in (self.status_label, self.progress_label, self.summary_label):
            label.configure(wraplength=width)

    def _label(self, parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(parent, text=text, style="Archive.TLabel", width=9).grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=(2, 5))

    def _show_ready(self, ready: bool) -> None:
        self._ready = ready
        if ready:
            self.setup_frame.grid_remove()
            self.ready_host.grid()
        else:
            self.ready_host.grid_remove()
            self.setup_frame.grid()
        self._update_controls()

    def _update_controls(self) -> None:
        enabled = self._ready and not self._busy and not self._closing
        for widget in self.controls:
            widget.configure(state="normal" if enabled else "disabled")
        self.preset_box.configure(state="readonly" if enabled else "disabled")
        self.chat_tree.configure(selectmode="browse" if enabled else "none")
        self.initialize_button.configure(state="normal" if not self._busy and not self._closing else "disabled")
        self.account_box.configure(state="readonly" if not self._busy and not self._closing else "disabled")
        self.cancel_button.configure(state="normal" if self._busy and not self._closing else "disabled")
        self.export_button.configure(state="normal" if enabled and bool(self.chat_tree.selection()) else "disabled")
        self.verify_button.configure(state="normal" if enabled and bool(self.chat_tree.selection()) else "disabled")
        if not self.voice.get() or not enabled:
            self.asr_check.configure(state="disabled")
            self.preset_box.configure(state="disabled")
        elif not self.transcribe.get():
            self.preset_box.configure(state="disabled")
        self.open_button.configure(state="normal" if self._last_directory and self._last_directory.is_dir() else "disabled")

    def _voice_changed(self) -> None:
        if not self.voice.get():
            self.transcribe.set(False)
        self._update_controls()

    def _save_settings(self) -> None:
        output = self.output.get().strip()
        if output:
            try:
                save_ui_config(UiConfig(output_root=output, asr_preset=self.preset.get()))
            except (OSError, ValueError) as exc:
                self.status_text.set(f"配置保存失败：{exc}")

    def _load(self, check, progress):
        status = self.service.status()
        check()
        if status.ready:
            return status, self.service.list_chats(), []
        return status, [], self.service.accounts()

    def _start(self, kind: str, operation) -> None:
        if self._busy or self._closing:
            return
        self._task = kind
        self._busy = True
        self.progress_bar["value"] = 0
        self.progress_text.set("正在处理…")
        self.summary_text.set("")
        self.status_text.set({"load": "正在读取本地状态…", "initialize": "正在初始化…", "export": "正在导出…", "verify": "正在检查归档…"}.get(kind, "正在处理…"))
        self._update_controls()
        try:
            self.worker.start(operation)
        except RuntimeError as exc:
            self._busy = False
            self.status_text.set(str(exc))
            self._update_controls()

    def _poll(self) -> None:
        self._poll_id = None
        for event in self.worker.drain():
            self._handle_event(event)
        if self._closing and not self._busy:
            self.root.destroy()
            return
        self._poll_id = self.root.after(80, self._poll)

    def _handle_event(self, event: WorkerEvent) -> None:
        if event.kind == "progress":
            current, total, label = event.value
            percent = min(100, current * 100 / total) if total else 0
            self.progress_text.set(f"{percent:.0f}%  {label}" if total else label)
            self.progress_bar["value"] = percent
            return
        self.worker.join()
        self._busy = False
        if event.kind == "done":
            if self._task in {"load", "initialize"}:
                status, chats, accounts = event.value
                self.account_text.set(status.account)
                self._accounts = [str(item["account"]) for item in accounts]
                self.account_box.configure(values=self._accounts)
                if len(self._accounts) == 1:
                    self.account_choice.set(self._accounts[0])
                elif status.account in self._accounts:
                    self.account_choice.set(status.account)
                self._show_ready(status.ready)
                if status.ready:
                    self._chats = {str(c["username"]): c for c in chats}
                    self._filter_chats()
                    self.status_text.set("READY · 本地数据库可用")
                else:
                    self.status_text.set("需要初始化 · 请先登录 Windows 微信")
            elif self._task == "export":
                result = event.value
                self._last_directory = result.archive_path.parent
                self.summary_text.set(f"{result.message_count} 条消息 · {result.attachment_count} 个附件 · {result.transcript_count} 条转写" + (f" · {len(result.warnings)} 条警告" if result.warnings else ""))
                self.status_text.set("DONE · 归档已保存")
                if result.warnings:
                    self.progress_text.set(result.warnings[0])
            elif self._task == "verify":
                result = event.value
                self.summary_text.set(f"{result.message_count} 条消息 · {result.attachment_count} 个附件 · 语音转写 {result.voice_transcript_count}/{result.voice_count}" + (f" · {result.orphan_count} 个未引用文件" if result.orphan_count else ""))
                self.status_text.set("PASS · 完整性检查通过" if result.ok else "FAIL · 完整性检查未通过")
                if result.errors:
                    self.progress_text.set(result.errors[0])
                self._last_directory = result.root
            if self._task in {"export", "verify"}:
                self.progress_bar["value"] = 100
        elif event.kind == "cancelled":
            self.status_text.set("已取消 · 原有归档保留，可再次导出")
            self.progress_text.set("")
        else:
            self.status_text.set(f"失败：{event.value}")
            self.progress_text.set("")
        self._update_controls()

    def _filter_chats(self) -> None:
        needle = self.search.get().strip().casefold()
        selected = self.chat_tree.selection()
        previous = selected[0] if selected else ""
        self.chat_tree.delete(*self.chat_tree.get_children())
        for chat in self._chats.values():
            name = str(chat.get("name") or chat["username"])
            username = str(chat["username"])
            if needle and needle not in name.casefold() and needle not in username.casefold():
                continue
            self.chat_tree.insert("", "end", iid=username, values=(name, chat.get("message_count", 0)))
        if previous and self.chat_tree.exists(previous):
            self.chat_tree.selection_set(previous)
        elif needle and len(self.chat_tree.get_children()) == 1:
            self.chat_tree.selection_set(self.chat_tree.get_children()[0])
        self._selection_changed()

    def _selected_chat(self) -> dict | None:
        selection = self.chat_tree.selection()
        return self._chats.get(selection[0]) if selection else None

    def _selection_changed(self) -> None:
        self._update_controls()

    def initialize(self) -> None:
        account = self.account_choice.get().strip() or None
        if len(self._accounts) > 1 and account is None:
            self.status_text.set("请先选择要初始化的微信账号")
            return
        def operation(check, progress):
            status = self.service.initialize(account=account)
            check()
            return status, self.service.list_chats(), []
        self._start("initialize", operation)

    def browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, initialdir=self.output.get() or str(Path.home()), mustexist=True)
        if chosen:
            self.output.set(chosen)
            self._save_settings()

    def _target(self) -> Path:
        chat = self._selected_chat()
        if chat is None:
            raise ValueError("请先选择会话")
        if not self.output.get().strip():
            raise ValueError("请选择输出目录")
        return self.service.destination(Path(self.output.get()), chat)

    def start_export(self) -> None:
        if self._busy or not self._ready:
            return
        try:
            chat = self._selected_chat()
            target = self._target()
            start, end = parse_date(self.start_date.get().strip()), parse_date(self.end_date.get().strip())
            if start and end and start > end:
                raise ValueError("开始日期不能晚于结束日期")
            media_types = frozenset(({3} if self.images.get() else set()) | ({34} if self.voice.get() else set()) | ({43, 49} if self.other_media.get() else set()))
            transcribe = self.transcribe.get() and 34 in media_types
            preset = self.preset.get()
            self._save_settings()
        except (OSError, ValueError) as exc:
            self.status_text.set(str(exc))
            return
        self._start("export", lambda check, progress: self.service.export(chat_selector=str(chat["username"]), output_dir=target, start=start, end=end, media_types=media_types, transcribe=transcribe, asr_preset=preset, progress=progress, cancel=check))

    def start_verify(self) -> None:
        if self._busy or not self._ready:
            return
        try:
            target = self._target()
        except (OSError, ValueError) as exc:
            self.status_text.set(str(exc))
            return
        def operation(check, progress):
            check()
            result = self.service.verify(target)
            check()
            return result
        self._start("verify", operation)

    def cancel(self) -> None:
        if self._busy:
            self.worker.cancel()
            self.status_text.set("正在取消 · 等待当前操作结束…")
            self.cancel_button.configure(state="disabled")

    def open_folder(self) -> None:
        if self._last_directory and self._last_directory.is_dir():
            try:
                os.startfile(str(self._last_directory))
            except OSError as exc:
                self.status_text.set(f"无法打开目录：{exc}")

    def close(self) -> None:
        if self._busy:
            self._closing = True
            self.cancel()
            self.status_text.set("正在安全退出 · 等待当前操作结束…")
            self._update_controls()
            return
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
        self.root.destroy()


def main(*, smoke: bool = False) -> int:
    root = tk.Tk()
    if smoke:
        root.withdraw()
    window = ArchiveWindow(root, autoload=not smoke)
    if smoke:
        root.update()
        window.close()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
